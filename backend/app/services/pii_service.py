"""Five-stage PII redaction: regex → IP/URL → Presidio NER → second-pass → confidence.

Presidio is imported lazily so app boot does not require the spaCy model.

**The detector has a readiness state and the chassis fails closed on it**
(blueprint S4c, gap H15). Stage 3 is the only stage that finds a person,
a place or an organisation; the four regex stages cannot. When Presidio
fails to initialise (no spaCy model) or ``analyze`` raises, this module
used to return its input and let ``redact`` carry on through the regex
stages — so every named-entity class vanished from the pipeline with
nothing said, at every attach point, and "unredacted content never
touches the database" became a claim about a code path that was no
longer running.

It now records a state — ``ready``, ``unavailable`` (initialisation
failed, with the exception's class) or ``failed`` (a call raised, with
the count) — warmed by :func:`warm_detector` in the app's lifespan so
``/health`` reports it before the first intake rather than on the first
call. Outside ``ready`` every content redaction raises
:class:`PiiDetectorUnavailable` and each attach point turns that into
its own refusal (503 at intake, the preview and the upload; a boundary
refusal for agent output; an MCP error; a stripped span or a dropped log
record on the way out).

``LIBRERUN_PII_ALLOW_DEGRADED=true`` is the operator's explicit opt-out:
it restores the regex-only pass-through, announces itself on the
platform plane at startup, stamps ``librerun.pii.degraded=true`` on
every span and record it touched, and writes an
``action_type="pii_detector_degraded"`` audit row for the tenant it
served that way.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

import structlog

from app.config import settings

logger = structlog.get_logger(__name__)

# An email address at ANY top-level domain. Stage 3's recognizer validates
# the domain against the public-suffix list, so it drops exactly the
# addresses enterprise logs carry most: `jsmith@corp.local` (an Active
# Directory UPN), `ops@acme.internal`, `alice@acme.corp`. Measured at S10
# on the release candidate: an intake carrying one address at each of
# those three domains stored all three raw in Postgres, while `@….com` was
# caught. The identifier check below always used this pattern, so a name
# was refused for what content kept. Stage 1 runs it first, before PHONE
# can take a digit run out of a local part and leave the domain behind.
# The TLD must be letters, so a package version (`openai@4.0.71`) is not
# an address.
_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# --- Stage 1: common PII regex patterns (high confidence) ---
STAGE1_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("EMAIL_ADDRESS", _EMAIL_PATTERN, 0.95),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), 0.95),
    ("CREDIT_CARD", re.compile(r"\b(?:\d[ -]*?){13,19}\b"), 0.80),
    ("PHONE", re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), 0.85),
    ("API_KEY", re.compile(r"\b(?:sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]+)\b"), 0.95),
]

# --- Stage 2: IP / URL ---
STAGE2_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), 0.90),
    ("IPV6", re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b"), 0.90),
    ("URL", re.compile(r"https?://[^\s\"'<>]+"), 0.85),
]

# --- Stage 4: second-pass regex (broader patterns) ---
STAGE4_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("BASE64_BLOB", re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), 0.75),
    ("CONN_STRING", re.compile(r"\b(?:jdbc|mongodb|postgres(?:ql)?|mysql|redis)://[^\s\"'<>]+", re.IGNORECASE), 0.90),
]


@dataclass
class Redaction:
    pii_type: str
    placeholder: str
    confidence: float


# ---------------------------------------------------------------------------
# The detector's readiness state (blueprint S4c, gap H15)
# ---------------------------------------------------------------------------
#
# Three states and nothing finer, because the policy only ever asks one
# question — did stage 3 run over this text?
#
#   ready       — the analyzer exists and answered the startup probe;
#                 coverage is ``ner``.
#   unavailable — the analyzer could not be built (no spaCy model, a
#                 broken install). Carries the constructor's exception
#                 CLASS, never its message: a message can quote a path.
#   failed      — the analyzer exists but a call raised. Carries how many
#                 times. A later successful call returns the state to
#                 ``ready`` and leaves the count, so ``/health`` still
#                 shows that something went wrong without a transient
#                 fault becoming an outage that only a restart clears.
#
# ``unavailable`` does not self-heal, because ``_get_presidio`` caches
# the failure; ``warm_detector(force=True)`` is the way back.
READY = "ready"
UNAVAILABLE = "unavailable"
FAILED = "failed"

COVERAGE_NER = "ner"
COVERAGE_REGEX_ONLY = "regex_only"

# The reason code every attach point reports. One spelling, so the 503
# body, the MCP error, the boundary refusal and the docs all say the
# same word.
UNAVAILABLE_CODE = "pii_detector_unavailable"

# What the opt-out leaves behind: a span attribute, a log-record field
# and an audit row.
DEGRADED_ATTRIBUTE = "librerun.pii.degraded"
DEGRADED_EVENT_FIELD = "librerun_pii_degraded"
DEGRADED_ACTION_TYPE = "pii_detector_degraded"

# The startup probe. It has to contain something ONLY stage 3 can find —
# a person and a place — or a probe that "passed" would prove nothing
# about the stage this whole module's policy is about. It is a fixture,
# not anybody's data.
_PROBE_TEXT = "Jane Roe met Dr. Alan Poole in Seattle."
# …and the probe has to LOOK at what came back, which is the other half
# of that sentence and was missing. ``analyze`` returning an empty list
# is not an error: an ``AnalyzerEngine`` whose registry ended up with no
# recognizers for this language, or whose spaCy pipeline was loaded
# without its ``ner`` component, builds and answers and finds nothing.
# Reporting that as ``ready`` is a gate that succeeds by not looking —
# and worse than no gate here, because every attach point then persists,
# forwards and exports text on the strength of it. One entity of the
# class the probe carries is the whole demand; WHICH one is not fixed,
# so a model that reads "Dr. Alan Poole" as an organisation still
# passes. What cannot pass is finding nothing at all.
_PROBE_MIN_ENTITIES = 1


class DetectorProbeFoundNothing(RuntimeError):
    """The analyzer built and answered the startup probe with no
    entities, so the named-entity stage is not really running."""

# How often a degradation may speak. The notice is itself a log record,
# which the log walk redacts, which calls back in here — so without a
# bound a single broken detector writes one line per line forever. The
# re-entrancy guard below stops the immediate recursion; this stops the
# slow version of it across the queue thread.
_NOTICE_INTERVAL_SECONDS = 60.0
# How many tenants' degradation notices are remembered for that
# throttle. An audit row is per tenant — a global throttle would let one
# busy tenant swallow another's row — and the map is bounded so a
# long-lived process cannot grow one entry per tenant forever.
_NOTICE_TENANTS_TRACKED = 256
# How many audit rows may wait for a database. Degradation is an
# operator-visible fault, not a metric: a bounded queue that drops with
# a count is honest, an unbounded one is a leak.
_AUDIT_QUEUE_MAX = 100

_analyzer = None
_anonymizer = None

_state_lock = threading.Lock()
_state: str | None = None  # None: not determined yet — never "ready"
_state_error: str | None = None  # the exception's CLASS name, never its text
_call_failures = 0
_degraded_calls = 0

# Re-entrancy: the notice below logs, the log walk redacts that record,
# the redaction lands back here. Per thread, because the emitting thread
# and the logging queue's thread are different ones.
_notice_guard = threading.local()
_notice_at: dict[str | None, float] = {}
_pending_audits: list[dict] = []
_audit_dropped = 0
_audit_tasks: set = set()


@dataclass
class DetectorStatus:
    """What the detector is, as ``/health`` and the audit row report it."""

    state: str
    coverage: str
    error: str | None = None
    failures: int = 0
    allow_degraded: bool = False

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "coverage": self.coverage,
            "error": self.error,
            "failures": self.failures,
            "allow_degraded": self.allow_degraded,
        }


class PiiDetectorUnavailable(RuntimeError):
    """The named-entity detector is not ready and the operator has not
    opted into degraded redaction, so the chassis refuses.

    Carries the state, the attach point that refused and the failing
    exception's CLASS — never any of the text it was asked to redact.
    """

    code = UNAVAILABLE_CODE

    def __init__(self, state: str, *, stage: str, error: str | None = None):
        self.state = state
        self.stage = stage
        self.error = error
        detail = f" ({error})" if error else ""
        super().__init__(
            f"{UNAVAILABLE_CODE}: the PII detector is {state}{detail}; "
            f"refusing {stage}"
        )


@dataclass
class Degradation:
    """What :func:`observe_degradation` collects: whether the detector
    ran regex-only inside the block, and how often. The reasons are
    state names, never text."""

    count: int = 0
    states: set = field(default_factory=set)

    @property
    def degraded(self) -> bool:
        return self.count > 0


_observation: ContextVar[Degradation | None] = ContextVar(
    "librerun_pii_degradation", default=None
)


@contextmanager
def observe_degradation():
    """Collect the degraded redactions performed inside this block.

    The telemetry walks use it to decide whether the span or record they
    just walked has to carry the ``librerun.pii.degraded`` stamp: the
    walk calls :func:`redact` in many positions and needs one answer for
    the container it is about to hand on.
    """
    observation = Degradation()
    token = _observation.set(observation)
    try:
        yield observation
    finally:
        _observation.reset(token)


def allow_degraded() -> bool:
    """The operator's explicit opt-out. Read on every call, never cached:
    a test (and an admin restart) changes it, and a cached "closed" would
    make the opt-out a lie in the other direction."""
    return bool(getattr(settings, "LIBRERUN_PII_ALLOW_DEGRADED", False))


def _set_state(state: str, *, error: str | None = None) -> None:
    global _state, _state_error
    with _state_lock:
        _state = state
        if state == READY:
            _state_error = None
        elif error is not None:
            _state_error = error


def _get_presidio():
    global _analyzer, _anonymizer
    if _analyzer is None:
        try:
            from presidio_analyzer import AnalyzerEngine  # type: ignore
            from presidio_anonymizer import AnonymizerEngine  # type: ignore

            _analyzer = AnalyzerEngine()
            _anonymizer = AnonymizerEngine()
        except Exception as exc:  # noqa: BLE001
            _analyzer = False  # sentinel: unavailable
            _anonymizer = False
            _set_state(UNAVAILABLE, error=type(exc).__name__)
    if _analyzer is False:
        return None, None
    # A live engine and no state yet means nothing has determined one —
    # the constructor just worked, or a caller reset the state without
    # dropping the cached engine. Either way the claim that belongs here
    # is ``ready``: whether ``analyze`` works is what the probe in
    # :func:`warm_detector` answers, and a raising call downgrades this
    # to ``failed`` on the spot. Outside the `if` above on purpose —
    # inside it, the second case returned ``unavailable`` for an engine
    # that was working, which fails closed for no reason at all.
    if _state is None:
        _set_state(READY)
    return _analyzer, _anonymizer


def _ensure_determined() -> str:
    """The state, determining it lazily if nothing warmed the process.

    A script, a test or an agent package that imports this module
    without a lifespan still gets a truthful answer rather than an
    optimistic one — and the first call pays for it, which is exactly
    what :func:`warm_detector` exists to move to startup.
    """
    if _state is None:
        _get_presidio()
    return _state or UNAVAILABLE


def detector_status() -> DetectorStatus:
    """The readiness state, for ``/health`` and for the audit row.

    Never warms a detector it finds already determined, and never
    re-probes: ``/health`` reports the state, it does not create it.
    """
    state = _ensure_determined()
    return DetectorStatus(
        state=state,
        coverage=COVERAGE_NER if state == READY else COVERAGE_REGEX_ONLY,
        error=_state_error,
        failures=_call_failures,
        allow_degraded=allow_degraded(),
    )


def warm_detector(*, force: bool = False) -> DetectorStatus:
    """Build the analyzer and prove it answers, at startup.

    Called from the app's (and the gateway's) lifespan. It never raises:
    a detector that cannot be built is a state to report, not a reason
    to refuse to boot — the refusing is done per request, where an
    operator can see which endpoint said no and why.

    "Prove it answers" is meant literally: the probe's RESULT is
    checked, not merely the absence of an exception. An engine that
    builds and recognises nothing answers every call successfully and
    removes no name, which is the one failure this state exists to
    report and the one a probe that ignored its return value would call
    ``ready``.
    """
    global _analyzer, _anonymizer, _state
    if force:
        with _state_lock:
            _analyzer = None
            _anonymizer = None
            _state = None
    analyzer, _ = _get_presidio()
    if analyzer is not None:
        try:
            found = analyzer.analyze(text=_PROBE_TEXT, language="en")
        except Exception as exc:  # noqa: BLE001
            _record_call_failure(exc)
        else:
            _record_probe(found)
    status = detector_status()
    if status.state == READY:
        logger.info(
            "pii_detector_ready", state=status.state, coverage=status.coverage
        )
    elif status.allow_degraded:
        # The opt-out announces itself, once, where an operator reading
        # the platform plane will see it next to the boot line.
        logger.warning(
            "pii_detector_degraded_allowed",
            state=status.state,
            coverage=status.coverage,
            error=status.error,
            variable="LIBRERUN_PII_ALLOW_DEGRADED",
        )
    else:
        logger.error(
            "pii_detector_unavailable",
            state=status.state,
            coverage=status.coverage,
            error=status.error,
        )
    return status


def _record_probe(found) -> None:
    """What the startup probe came back with.

    One entity is the whole demand, and WHICH one is not fixed: a model
    that reads ``Dr. Alan Poole`` as an organisation still passes. What
    cannot pass is finding nothing at all.

    A blind engine is DROPPED rather than merely recorded, onto the same
    ``False`` sentinel a constructor that raised leaves behind. Recording
    the state alone would not have been enough: every refusal in this
    module is decided by ``_get_presidio`` returning nothing or by
    ``analyze`` raising, and an engine that answers every call with an
    empty list does neither — the chassis would go on persisting,
    forwarding and exporting on the strength of a state nothing reads at
    the call site. The sentinel is read by all of them, so this needs no
    new condition anywhere else.

    ``unavailable`` and not ``failed``: nothing raised, and the count
    ``failed`` carries is a count of calls that did.
    """
    global _analyzer, _anonymizer
    if len(found) >= _PROBE_MIN_ENTITIES:
        _set_state(READY)
        return
    _analyzer = False
    _anonymizer = False
    _set_state(UNAVAILABLE, error=DetectorProbeFoundNothing.__name__)


def _record_call_failure(exc: BaseException) -> None:
    global _call_failures
    with _state_lock:
        _call_failures += 1
    _set_state(FAILED, error=type(exc).__name__)


def _record_call_success() -> None:
    # A detector that answers again is ready again; the failure COUNT
    # survives so ``/health`` keeps showing that it once did not.
    if _state == FAILED:
        _set_state(READY)


def _current_tenant() -> str | None:
    try:
        tenant = structlog.contextvars.get_contextvars().get("tenant_id")
    except Exception:  # noqa: BLE001
        return None
    return str(tenant) if tenant else None


def _throttled(tenant: str | None) -> bool:
    """True when this tenant's degradation has already been reported
    inside the interval. Bounded: the oldest entries go first."""
    now = time.monotonic()
    last = _notice_at.get(tenant)
    if last is not None and now - last < _NOTICE_INTERVAL_SECONDS:
        return True
    if len(_notice_at) >= _NOTICE_TENANTS_TRACKED:
        for key in sorted(_notice_at, key=lambda k: _notice_at[k])[
            : len(_notice_at) - _NOTICE_TENANTS_TRACKED + 1
        ]:
            _notice_at.pop(key, None)
    _notice_at[tenant] = now
    return False


def _stamp_current_span() -> None:
    """Stamp the span this degradation happened under.

    On the request path that is the endpoint's span, inside a run it is
    the phase span — the ones an operator looks at when asking which
    work was served regex-only. The export path stamps its own copies
    (``observability/otlp_walk.py``), because a span already ended
    cannot be written to.
    """
    try:
        from opentelemetry import trace as _trace

        span = _trace.get_current_span()
        if span is not None and span.is_recording():
            span.set_attribute(DEGRADED_ATTRIBUTE, True)
    except Exception:  # noqa: BLE001 — telemetry never breaks redaction
        pass


def _note_degraded(*, stage: str) -> None:
    """Record one degraded pass: the counter, the observation, the span
    stamp, and — throttled, per tenant — the log line and the audit row."""
    global _degraded_calls
    with _state_lock:
        _degraded_calls += 1
    observation = _observation.get()
    if observation is not None:
        observation.count += 1
        observation.states.add(_state or UNAVAILABLE)
    _stamp_current_span()
    if getattr(_notice_guard, "busy", False):
        # We are inside the walk of our OWN notice. Counting it is
        # right; saying it again is how one broken detector becomes an
        # unbounded log.
        return
    _notice_guard.busy = True
    try:
        tenant = _current_tenant()
        if _throttled(tenant):
            return
        status = detector_status()
        logger.warning(
            "pii_detector_degraded",
            stage=stage,
            state=status.state,
            coverage=status.coverage,
            error=status.error,
            failures=status.failures,
        )
        _queue_degraded_audit(tenant, stage, status)
    finally:
        _notice_guard.busy = False


def _queue_degraded_audit(tenant: str | None, stage: str, status: DetectorStatus) -> None:
    """Queue the ``pii_detector_degraded`` row and try to write it now.

    A row is per tenant, and tenant scoping is not optional: a
    degradation with no tenant bound (a startup probe, the logging
    thread) is reported on the platform plane above and written nowhere.
    """
    global _audit_dropped
    if tenant is None:
        return
    if len(_pending_audits) >= _AUDIT_QUEUE_MAX:
        _audit_dropped += 1
        return
    _pending_audits.append(
        {
            "tenant_id": tenant,
            "detail": {"stage": stage, **status.as_dict()},
        }
    )
    try:
        import asyncio

        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No event loop on this thread (the logging queue, a batch span
        # processor): the row waits for the next caller that has one.
        return
    task = loop.create_task(flush_degraded_audits())
    _audit_tasks.add(task)
    task.add_done_callback(_audit_tasks.discard)


async def flush_degraded_audits() -> int:
    """Persist the queued ``pii_detector_degraded`` rows; the count written.

    Draining is one synchronous swap, so two concurrent flushes cannot
    write the same row twice.
    """
    pending, _pending_audits[:] = list(_pending_audits), []
    if not pending:
        return 0
    from uuid import UUID

    written = 0
    try:
        from app.database import async_session
        from app.models import ActivityAuditLog

        async with async_session() as db:
            async with db.begin():
                for entry in pending:
                    db.add(
                        ActivityAuditLog(
                            tenant_id=UUID(entry["tenant_id"]),
                            user_id=None,
                            user_email=None,
                            action_type=DEGRADED_ACTION_TYPE,
                            detail=entry["detail"],
                        )
                    )
                    written += 1
    except Exception as exc:  # noqa: BLE001
        # The rows are gone rather than retried forever: a database that
        # is down is already the louder problem, and the platform-plane
        # warning above said the same thing without needing one.
        logger.warning(
            "pii_detector_degraded_audit_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            rows=len(pending),
        )
        return 0
    return written


def _degrade_or_refuse(*, stage: str) -> None:
    """The policy, in one place: regex-only under the opt-out, a refusal
    otherwise."""
    if allow_degraded():
        _note_degraded(stage=stage)
        return
    raise PiiDetectorUnavailable(
        _state or UNAVAILABLE, stage=stage, error=_state_error
    )


def require_ready(*, stage: str) -> DetectorStatus:
    """Refuse the attach point outright when the detector is not ready.

    The gate the endpoints call BEFORE they do any work, so that intake
    refuses a payload with no ``x-pii`` field in it too: "no run can be
    created" is a property of the run, not of whether this particular
    body happened to carry text stage 3 would have walked.
    """
    state = _ensure_determined()
    if state != READY:
        _degrade_or_refuse(stage=stage)
    return detector_status()


def detector_counters() -> dict:
    """Counts for a test and for the admin surface; never any text."""
    return {
        "failures": _call_failures,
        "degraded_calls": _degraded_calls,
        "audit_queue": len(_pending_audits),
        "audit_dropped": _audit_dropped,
    }


def _reset_detector_for_tests() -> None:
    """Forget every cached decision. Tests only — injecting a broken
    constructor means nothing if the engine from the last test is still
    in the module global."""
    global _analyzer, _anonymizer, _state, _state_error
    global _call_failures, _degraded_calls, _audit_dropped
    with _state_lock:
        _analyzer = None
        _anonymizer = None
        _state = None
        _state_error = None
        _call_failures = 0
        _degraded_calls = 0
        _audit_dropped = 0
    _notice_at.clear()
    _pending_audits.clear()
    _notice_guard.busy = False


def _mask(pii_type: str, n: int) -> str:
    return f"[REDACTED_{pii_type}_{n}]"


def _apply_stage(
    text: str,
    patterns: list[tuple[str, re.Pattern, float]],
    counters: dict[str, int],
    redactions: list[Redaction],
    threshold: float,
) -> str:
    def repl_factory(pii_type: str, confidence: float):
        def repl(match: re.Match) -> str:
            if confidence < threshold:
                return match.group(0)
            counters[pii_type] = counters.get(pii_type, 0) + 1
            placeholder = _mask(pii_type, counters[pii_type])
            redactions.append(Redaction(pii_type=pii_type, placeholder=placeholder, confidence=confidence))
            return placeholder

        return repl

    for pii_type, pattern, confidence in patterns:
        text = pattern.sub(repl_factory(pii_type, confidence), text)
    return text


def _apply_presidio(
    text: str,
    counters: dict[str, int],
    redactions: list[Redaction],
    threshold: float,
    skip_entities: frozenset[str] = frozenset(),
    *,
    stage: str = "redact",
) -> str:
    analyzer, _ = _get_presidio()
    if analyzer is None:
        # Stage 3 is the ONLY stage that finds a person, a place or an
        # organisation. Returning the input here — which is what this
        # did until S4c — drops those classes from every attach point
        # with nothing said. The policy decides instead: regex-only
        # under the operator's opt-out, a refusal otherwise.
        _degrade_or_refuse(stage=stage)
        return text
    try:
        results = analyzer.analyze(text=text, language="en")
    except Exception as exc:  # noqa: BLE001
        _record_call_failure(exc)
        _degrade_or_refuse(stage=stage)
        return text
    _record_call_success()
    if skip_entities:
        results = [r for r in results if r.entity_type not in skip_entities]
    # Two recognizers can claim overlapping spans (an address the NER also
    # reads as a location): keep one per region — the higher score, then
    # the longer match — or the second replacement lands on offsets the
    # first one moved and corrupts the text (blueprint S4 found it).
    kept: list = []
    for r in sorted(
        (r for r in results if r.score >= threshold),
        key=lambda r: (-r.score, -(r.end - r.start), r.start),
    ):
        if all(r.end <= k.start or r.start >= k.end for k in kept):
            kept.append(r)
    # Apply from the end so earlier offsets stay valid.
    results = sorted(kept, key=lambda r: r.start, reverse=True)
    for r in results:
        pii_type = r.entity_type
        counters[pii_type] = counters.get(pii_type, 0) + 1
        placeholder = _mask(pii_type, counters[pii_type])
        redactions.append(Redaction(pii_type=pii_type, placeholder=placeholder, confidence=float(r.score)))
        text = text[: r.start] + placeholder + text[r.end:]
    return text


# Entity types the AGENT BOUNDARY leaves in place (blueprint S4, a §12
# decision): intake redacts a date or a place name inside a free-text
# field a schema marks ``x-pii`` — a log line, an observation — where
# over-redaction costs nothing. An agent's terminal output and event text
# are the product: a report's timeline and a vendor's city are content,
# not a person's identity, and the walk exists for identities — emails,
# phone numbers, cards, social-security numbers, IPs, people. The
# ``pii`` capability's ``redact`` is the intake pipeline unchanged.
BOUNDARY_SKIP_ENTITIES: frozenset[str] = frozenset({"DATE_TIME", "LOCATION"})


def redact(
    text: str,
    threshold: float | None = None,
    *,
    skip_entities: frozenset[str] = frozenset(),
    quiet: bool = False,
    stage: str = "redact",
) -> tuple[str, list[Redaction]]:
    """Redact one text. ``quiet`` skips the ``pii_redaction_applied`` line
    — the log and span walkers redact on the logging pipeline's own
    thread, where that line would be one more record per record.

    Raises :class:`PiiDetectorUnavailable` when stage 3 could not run and
    the operator has not set ``LIBRERUN_PII_ALLOW_DEGRADED`` — every
    caller of this function is a persist or an export point, and
    returning half-redacted text to one of them is the whole of gap H15.
    ``stage`` names the attach point in the refusal and the audit row.
    """
    threshold = threshold if threshold is not None else settings.PII_CONFIDENCE_THRESHOLD
    counters: dict[str, int] = {}
    redactions: list[Redaction] = []

    # Stage 1: regex
    text = _apply_stage(text, STAGE1_PATTERNS, counters, redactions, threshold)
    # Stage 2: IP / URL
    text = _apply_stage(text, STAGE2_PATTERNS, counters, redactions, threshold)
    # Stage 3: Presidio NER (emails, names, etc.)
    text = _apply_presidio(
        text, counters, redactions, threshold, skip_entities, stage=stage
    )
    # Stage 4: second-pass regex
    text = _apply_stage(text, STAGE4_PATTERNS, counters, redactions, threshold)

    if redactions and not quiet:
        # stage_counts uses PII-type aggregation, not raw positions — nothing
        # here reveals the original content.
        logger.info(
            "pii_redaction_applied",
            stage_counts=dict(counters),
            total_redactions=len(redactions),
        )

    return text, redactions


# ---------------------------------------------------------------------------
# The walker (blueprint S4, gaps H7/H9/H10): one function for every
# agent-supplied value the chassis persists, forwards or exports — terminal
# output, audit and run-store arguments, event text, telemetry, and (S4a)
# the gateway's request positions.
#
# Three classes of position, decided by the caller's data shape:
#
# - **content**   — string leaves: redacted in place through ``redact``.
# - **identifier** — object keys and the positions a caller declares as
#   names (a run-store key, a step id, an action type, a trace state):
#   checked as their literal text and never rewritten, because rewriting
#   a key or a name changes the value's contract or collides with another;
#   a flagged identifier refuses the write.
# - **number**    — integers, and integral finite doubles below 2**53
#   (``2125551234.0`` is the same phone number; a double with a fractional
#   part is no card and no phone by construction): their decimal text goes
#   only to recognizers that can VALIDATE it — the stage-1 regexes above
#   exist for free text and match any bare run of digits, so every epoch
#   timestamp and most ids would have failed them. First Luhn: a 13–19
#   digit value the checksum accepts is a card, unless it has the timestamp
#   shape — 13, 16 or 19 digits with a leading ``1`` (epoch milliseconds,
#   microseconds or nanoseconds from 2001 to 2033, or a snowflake id that
#   begins with one; the ISO 7812 major industry identifier ``1`` belongs
#   to airline issuers whose one live scheme, UATP, issues 15-digit
#   numbers, so no issued card has that shape) AND its key path carries a
#   time or identifier context word; a ten-digit leading-``1`` value is
#   epoch seconds and never a NANP number. Then a phone number is flagged
#   when libphonenumber finds it valid under either parse the walker
#   performs — national for ``PII_PHONE_REGION``, or international with a
#   ``+`` prepended (a JSON number cannot carry the sign) — wherever it
#   sits; a phone context word on the key path only raises the confidence.
#   Nine bare digits are a social-security number only under a
#   social-security context word, because nine digits carry no checksum
#   or number plan and context is the only signal. A flagged number
#   refuses the write. The documented footgun is an identifier that IS a
#   valid phone number of the region (in the NANP, ten digits beginning
#   2–9 with a valid exchange); the refusal names its path.
# ---------------------------------------------------------------------------

_TIME_OR_ID_TOKENS = frozenset(
    {"ts", "at", "created", "updated", "expires", "expiry", "epoch", "id", "uuid", "guid"}
)
_TIME_OR_ID_SUBSTRINGS = ("time", "date", "epoch")
_PHONE_TOKENS = frozenset({"phone", "tel", "telephone", "mobile", "cell", "fax", "msisdn"})
_SSN_TOKENS = frozenset({"ssn", "social", "socialsecurity"})

_CARD_LENGTHS = range(13, 20)
_TIMESTAMP_SHAPE_LENGTHS = frozenset({13, 16, 19})
_INTEGRAL_DOUBLE_LIMIT = 2**53

# Guards on the shape of what an agent hands the chassis; a deeper or
# wider value is refused as unwalkable rather than walked partially.
WALK_MAX_DEPTH = 64
WALK_MAX_NODES = 200_000


@dataclass
class Finding:
    """One thing the walker found: where (a JSON path, never the value),
    which class of position, the PII type, the confidence, and what was
    done — ``redacted`` for content, ``refused`` for identifiers and
    numbers."""

    path: str
    kind: str  # content | identifier | number
    pii_type: str
    confidence: float
    action: str  # redacted | refused


@dataclass
class WalkResult:
    value: object
    findings: list[Finding]

    @property
    def refusals(self) -> list[Finding]:
        return [f for f in self.findings if f.action == "refused"]

    @property
    def refused(self) -> bool:
        return any(f.action == "refused" for f in self.findings)


class PiiRefused(ValueError):
    """A write the walker refused. Carries the reason code the caller
    reports (``pii_in_output``, ``pii_in_audit``, ``pii_in_store``,
    ``pii_in_progress``, …), the argument and the JSON path — and never
    the value."""

    def __init__(self, reason: str, argument: str, finding: Finding):
        self.reason = reason
        self.argument = argument
        self.finding = finding
        super().__init__(
            f"{reason}: {argument} at {finding.path} "
            f"({finding.kind} flagged as {finding.pii_type})"
        )


class UnwalkableValue(ValueError):
    """The value is deeper or wider than the walker's guards allow."""


def _key_tokens(key: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", str(key).lower()) if t]


def _has_time_or_id_context(keys: tuple) -> bool:
    for key in keys:
        for tok in _key_tokens(key):
            if tok in _TIME_OR_ID_TOKENS or tok.endswith("id"):
                return True
            if any(s in tok for s in _TIME_OR_ID_SUBSTRINGS):
                return True
    return False


def _has_token(keys: tuple, tokens: frozenset) -> bool:
    return any(tok in tokens for key in keys for tok in _key_tokens(key))


def luhn_valid(digits: str) -> bool:
    """The ISO/IEC 7812 checksum over a decimal string."""
    if not digits or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _phone_valid(text: str, region: str | None) -> bool:
    try:
        import phonenumbers
    except ImportError:  # pragma: no cover — a hard dependency of presidio
        return False
    try:
        parsed = phonenumbers.parse(text, region)
    except Exception:  # noqa: BLE001 — NumberParseException and friends
        return False
    return phonenumbers.is_valid_number(parsed)


def check_number_text(
    digits: str, keys: tuple = (), *, path: str = "$", phone_region: str | None = None
) -> Finding | None:
    """The walker's number rule over the decimal text of a number.

    Returns a refusing ``Finding`` or ``None``. Only a bare run of digits
    is examined — a sign, a fraction or a separator is not a number the
    rule knows.
    """
    if not digits or not digits.isdigit():
        return None
    n = len(digits)
    if n in _CARD_LENGTHS and luhn_valid(digits):
        timestamp_shape = n in _TIMESTAMP_SHAPE_LENGTHS and digits[0] == "1"
        if not (timestamp_shape and _has_time_or_id_context(keys)):
            return Finding(path, "number", "CREDIT_CARD", 0.9, "refused")
    if n == 10 and digits[0] == "1":
        # Epoch seconds; no NANP number starts with 1.
        return None
    region = phone_region or settings.PII_PHONE_REGION
    if _phone_valid(digits, region) or _phone_valid("+" + digits, None):
        confidence = 0.95 if _has_token(keys, _PHONE_TOKENS) else 0.85
        return Finding(path, "number", "PHONE", confidence, "refused")
    if n == 9 and _has_token(keys, _SSN_TOKENS):
        return Finding(path, "number", "SSN", 0.8, "refused")
    return None


# What an identifier is checked against: the STRUCTURAL recognizers — an
# email address, a dashed SSN, an API key, an IP, a URL, a connection
# string, a base64 blob — and, for every run of seven or more digits it
# carries, the number rule above with the identifier's own tokens as
# context (``created_1757534400000`` is a timestamp, ``1234567890123452``
# a card). Not the bare-digit regexes of stage 1, which would call every
# timestamp a card, and not the NER stage: a name-entity model reading a
# lone token such as ``ts_ns`` as a location is noise, not a finding.
# Stage 1's entries are looked up by NAME: this list once took them by
# index, and adding a pattern to stage 1 would have swapped what the
# identifier check means without a line of it changing.
_STAGE1 = {name: pattern for name, pattern, _ in STAGE1_PATTERNS}
_IDENTIFIER_PATTERNS: list[tuple[str, re.Pattern, float]] = [
    ("EMAIL_ADDRESS", _EMAIL_PATTERN, 0.95),
    ("SSN", _STAGE1["SSN"], 0.95),
    ("API_KEY", _STAGE1["API_KEY"], 0.95),
    *STAGE2_PATTERNS,
    *STAGE4_PATTERNS,
]
_DIGIT_RUN = re.compile(r"\d{7,}")


def check_identifier(
    text: str, keys: tuple = (), *, path: str = "$", phone_region: str | None = None
) -> Finding | None:
    """Check a name — an object key, a store key, a step id, an action
    type, a trace state — as its literal text. Never rewrites."""
    for pii_type, pattern, confidence in _IDENTIFIER_PATTERNS:
        if pattern.search(text):
            return Finding(path, "identifier", pii_type, confidence, "refused")
    context = keys + (text,)
    for run in _DIGIT_RUN.finditer(text):
        finding = check_number_text(
            run.group(0), context, path=path, phone_region=phone_region
        )
        if finding is not None:
            return Finding(path, "identifier", finding.pii_type, finding.confidence, "refused")
    return None


def walk(
    value,
    *,
    path: str = "$",
    keys: tuple = (),
    identifier: bool = False,
    phone_region: str | None = None,
    quiet: bool = False,
) -> WalkResult:
    """Walk a JSON-like value: strings redacted in place, object keys and
    declared identifiers checked, numbers checked by the number rule.
    Returns the walked copy and every finding; the caller decides what a
    refusal means for its write."""
    findings: list[Finding] = []
    budget = [WALK_MAX_NODES]

    def _visit(node, node_path: str, node_keys: tuple, depth: int, as_identifier: bool):
        budget[0] -= 1
        if budget[0] < 0:
            raise UnwalkableValue(f"more than {WALK_MAX_NODES} nodes")
        if depth > WALK_MAX_DEPTH:
            raise UnwalkableValue(f"deeper than {WALK_MAX_DEPTH} levels")
        if isinstance(node, bool) or node is None:
            return node
        if isinstance(node, str):
            if as_identifier:
                finding = check_identifier(
                    node, node_keys, path=node_path, phone_region=phone_region
                )
                if finding is not None:
                    findings.append(finding)
                return node
            redacted, applied = redact(node, skip_entities=BOUNDARY_SKIP_ENTITIES, quiet=quiet)
            for r in applied:
                findings.append(
                    Finding(node_path, "content", r.pii_type, float(r.confidence), "redacted")
                )
            return redacted
        if isinstance(node, int):
            if node >= 0:
                finding = check_number_text(
                    str(node), node_keys, path=node_path, phone_region=phone_region
                )
                if finding is not None:
                    findings.append(finding)
            return node
        if isinstance(node, float):
            if node.is_integer() and abs(node) < _INTEGRAL_DOUBLE_LIMIT and node >= 0:
                finding = check_number_text(
                    str(int(node)), node_keys, path=node_path, phone_region=phone_region
                )
                if finding is not None:
                    findings.append(finding)
            return node
        if isinstance(node, dict):
            out = {}
            for index, (key, child) in enumerate(node.items()):
                key_text = str(key)
                # A flagged key is addressed by its ordinal in the object,
                # never by its text: the path is what the refusal names,
                # and a key IS the value it must not name. Descendants of
                # a flagged key carry the same placeholder segment.
                masked_path = f"{node_path}.<key {index}>"
                finding = check_identifier(
                    key_text, node_keys + (key_text,), path=masked_path, phone_region=phone_region
                )
                if finding is not None:
                    findings.append(finding)
                    child_path = masked_path
                else:
                    child_path = f"{node_path}.{key_text}"
                out[key] = _visit(child, child_path, node_keys + (key_text,), depth + 1, False)
            return out
        if isinstance(node, (list, tuple)):
            return [
                _visit(item, f"{node_path}[{i}]", node_keys, depth + 1, False)
                for i, item in enumerate(node)
            ]
        # Anything else (a UUID, a datetime) is not agent-supplied JSON;
        # it passes as it is.
        return node

    walked = _visit(value, path, keys, 0, identifier)
    return WalkResult(walked, findings)


def walk_or_refuse(value, *, argument: str, reason: str, **kwargs):
    """``walk`` for a write: the walked value, or ``PiiRefused`` naming the
    reason code, the argument and the path of the first refusal."""
    result = walk(value, **kwargs)
    if result.refused:
        raise PiiRefused(reason, argument, result.refusals[0])
    return result.value
