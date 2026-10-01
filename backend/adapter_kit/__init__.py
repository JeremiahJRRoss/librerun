"""The adapter conformance battery (blueprint B16).

Every framework adapter — LangGraph first, the roadmap after it — has to
answer the same three questions before it can claim to run on LibreRun:

1. does it produce **schema-valid output** for a standard scenario,
2. does it **stream progress** while it works, and
3. does it **emit traces**?

Those are the platform's promises to a user, and they are the only
things the chassis cannot supply on an adapter's behalf. This module is
the salvaged descendant of the retired cross-runtime conformance suite
(blueprint §2.4): same idea — one battery, many targets — narrowed to
the contract that actually matters now.

It is deliberately framework-agnostic. The battery takes anything
implementing ``AgentProtocol`` and a scenario, so an adapter author runs
it without the kit knowing what a graph, crew, or workflow is::

    from adapter_kit import Scenario, run_battery

    result = await run_battery(my_agent, Scenario("demo", {...}))
    assert result.passed, result.failures

On the trace check: this asserts spans left the agent through the
process tracer provider, which is what an adapter controls. That they
then reach a collector is the *stack's* promise, proven separately and
end to end by the ``librerun-smoke`` workflow (OTLP → Vector → Jaeger).
Splitting it this way keeps the battery runnable on a laptop with no
containers, without either half pretending to prove the other.
"""
from __future__ import annotations

import json
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

# Safe at module level: `opentelemetry-sdk` is a chassis requirement and
# `app.services.agent_runner`, imported below, already pulls in the API.
from opentelemetry.sdk.trace import SpanProcessor

from app.agents.protocol import (
    AgentInput,
    AgentProtocol,
    AnalysisResult,
    InvestigationResult,
    StepProgress,
)

# Imported from the runner rather than restated here. The battery's job
# is to accept exactly what the chassis accepts; a second copy of this
# vocabulary would drift, and the drift would show up as a battery that
# certifies a phase the runner then marks failed.
from app.services.agent_runner import _SUCCESS_STATUSES as RUNNER_SUCCESS_STATUSES
# Likewise the intake pieces themselves, not reimplementations of them:
# the validator pins Draft 2020-12 regardless of what a schema's $schema
# says, and the redactor is what stands between a user's paste and the
# agent. Reimplementing either would let the battery certify a demo the
# production intake endpoint refuses, or run the adapter on input no
# production run ever produces.
from app.services.intake import (
    redact_pii_fields as _chassis_redact,
    validate_user_inputs as _chassis_validate,
)

# The chassis' progress vocabulary. An adapter that invents its own
# statuses breaks the run page, so the battery checks membership rather
# than merely that something was emitted.
VALID_STATUSES = {"pending", "running", "complete", "error", "skipped"}

# The manifest's `output.mode` vocabulary, which decides which field the
# run page reads. Kept in sync with the manifest's own Literal by the
# assertion in test_adapter_kit.py rather than by hoping.
_OUTPUT_MODES = {"html_report", "structured"}


@dataclass
class Scenario:
    """A standard input the adapter must be able to run."""

    name: str
    user_inputs: dict
    description: str = ""

    @classmethod
    def from_file(cls, path) -> "Scenario":
        """Load one of the agent's own ``scenarios/*.json`` files.

        Using the shipped scenario keeps the battery honest: it exercises
        the same input a user gets from the intake page's "Load scenario"
        control, not a hand-tuned one that happens to pass.
        """
        data = json.loads(open(path).read())
        return cls(
            name=data.get("name", str(path)),
            user_inputs=data.get("user_inputs", {}),
            description=data.get("description", ""),
        )


@dataclass
class BatteryResult:
    agent_id: str
    scenario: str
    phases_run: list[str] = field(default_factory=list)
    progress: list[tuple[str, str]] = field(default_factory=list)
    span_names: list[str] = field(default_factory=list)
    # Events recorded on those spans. An adapter that cannot honestly
    # wrap a unit of work in a span can still record that it happened,
    # and the battery should be able to see that.
    event_names: list[str] = field(default_factory=list)
    outputs: dict[str, Any] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    # How many PII redactions the chassis applied to the scenario before
    # the agent saw it. Reported rather than merely done, so an author
    # who is surprised by what their nodes received can see why.
    redactions: int = 0

    @property
    def passed(self) -> bool:
        return not self.failures

    def summary(self) -> str:
        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"[{verdict}] {self.agent_id} / {self.scenario}: "
            f"phases={self.phases_run} progress={len(self.progress)} "
            f"spans={len(self.span_names)}"
            + ("" if self.passed else "\n  - " + "\n  - ".join(self.failures))
        )


class _CaptureProcessor(SpanProcessor):
    """One reusable, switchable capture per provider.

    OTEL exposes no way to DETACH a span processor. The first version
    attached a fresh ``SimpleSpanProcessor`` per battery run and shut it
    down on the way out, which stopped it *retaining* spans — but the
    provider still holds every one of those dead processors and still
    calls ``on_end`` on each for every span the process finishes
    afterwards, some of them logging shutdown warnings. Ten battery runs
    left ten of them, so the cost of tracing grew with the number of
    conformance runs: an "inert" object is not a free one.

    So attach exactly one of these per provider and switch it on and off
    instead. ``_depth`` is a counter rather than a flag so that nested or
    concurrent battery runs in one process do not switch capture off
    from under each other; the buffer is released when the last one
    finishes. Sharing one buffer across concurrent runs is safe because
    ``_observed_spans`` filters by the run's own trace id — each run sees
    only its own spans regardless of what else is in here.
    """

    def __init__(self) -> None:
        self._spans: list = []
        self._depth = 0

    def begin(self) -> None:
        self._depth += 1

    def end(self) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._depth = 0
            self._spans = []

    # The exporter-shaped surface the battery reads, kept so callers do
    # not care whether they hold an exporter or a processor.
    def get_finished_spans(self) -> tuple:
        return tuple(self._spans)

    def clear(self) -> None:
        self._spans = []

    # --- SpanProcessor ------------------------------------------------
    def on_start(self, span, parent_context=None) -> None:
        pass

    def on_end(self, span) -> None:
        if self._depth > 0:
            self._spans.append(span)

    def shutdown(self) -> None:
        self._depth = 0
        self._spans = []

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


# Keyed weakly so a provider that goes out of scope takes its capturer
# with it rather than pinning it for the life of the process.
_CAPTURERS: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


@contextmanager
def _span_capture():
    """Capture spans for the duration of a battery run.

    Two cases:

    * An SDK provider is already installed (the app booted one). Attach
      **one** reusable ``_CaptureProcessor`` to it — never replace the
      provider, because that would silently detach the real exporter,
      and a battery that breaks the thing it measures is worse than no
      battery. Subsequent runs reuse the same processor rather than
      stacking up undetachable ones.

    * No SDK provider. Yield ``None`` — the caller is told spans could
      not be observed, and the kit installs nothing. See the comment
      below for why the two tidier-looking alternatives are both wrong.

    A battery that reports "couldn't observe spans" is honest; one that
    rearranges the process' tracing to look thorough is not.
    """
    from opentelemetry import trace as _trace
    from opentelemetry.sdk.trace import TracerProvider

    provider = _trace.get_tracer_provider()

    if not isinstance(provider, TracerProvider):
        # No SDK provider — so spans cannot be observed, and the kit
        # does NOT install one.
        #
        # Two earlier attempts were worse. Installing temporarily and
        # restoring is broken: OTEL's ProxyTracer caches the tracer it
        # resolved on first use, so only the first battery run in a
        # process would see anything. Installing permanently is worse
        # still: OTEL refuses later overrides, so a battery run before
        # an app's telemetry init would leave the app's REAL exporter
        # ignored for the life of the process — a conformance utility
        # silently disabling production tracing.
        #
        # Initializing tracing is the caller's decision, not a side
        # effect the kit is entitled to. Say so and let the caller act.
        yield None
        return

    capturer = _CAPTURERS.get(provider)
    if capturer is None:
        capturer = _CaptureProcessor()
        provider.add_span_processor(capturer)
        _CAPTURERS[provider] = capturer

    capturer.begin()
    try:
        yield capturer
    finally:
        # Switched off, not shut down and abandoned: this same object is
        # reused by the next run, so the provider never accumulates a
        # second one. Buffer released here (by `end`) once the last
        # concurrent run finishes.
        capturer.end()


# Deep enough for any real agent output; a hard floor under a state that
# references itself, which framework objects manage easily.
_MAX_WALK_DEPTH = 20

# redis-py's OWN encoder, so the emulation below cannot drift from the
# thing it emulates. Same rule this module already follows for the
# chassis' validator and redactor: import the real implementation
# rather than a description of it. Guarded because the module path is
# private and may move between redis-py versions.
try:  # pragma: no cover - exercised by whichever branch is installed
    from redis._parsers.encoders import Encoder as _RedisEncoder
except Exception:  # pragma: no cover
    _RedisEncoder = None

# ("utf-8", "strict", True) are the chassis' EFFECTIVE settings, not a
# guess: `app/redis.py` passes only `decode_responses=True` to
# `from_url`, leaving redis-py's own encoding defaults in place.
_REDIS_ENCODER = _RedisEncoder("utf-8", "strict", True) if _RedisEncoder else None


def _redis_encode(value: Any) -> None:
    """Encode a value exactly as the chassis' Redis client will.

    Hand-writing this call is precisely what the previous round got
    wrong. It used ``value.encode("utf-8")`` — ONE argument — where
    redis-py calls ``value.encode(self.encoding, self.encoding_errors)``
    — two. A subclass declaring ``def encode(self, encoding="utf-8")``
    accepts the first and raises ``TypeError`` on the second, so the
    battery passed a ``step_id`` that aborts every real phase. Measured
    against live async Redis, which raises exactly that ``TypeError``.

    The lesson is not "add an argument": it is that **an emulation
    written from a description drifts from the thing described**, and
    the comment above the broken call even quoted the correct
    two-argument signature. So call redis-py's encoder instead. The
    literal fallback exists only for the case where that private path
    moves, and is deliberately the narrower of the two.
    """
    if _REDIS_ENCODER is not None:
        _REDIS_ENCODER.encode(value)
        return
    if isinstance(value, str):
        value.encode("utf-8", "strict")


def _is_a(value, types) -> bool:
    """``isinstance`` that cannot raise — ``__class__`` is agent code.

    ``isinstance`` consults ``__class__``, and an agent can define that
    as a property that raises. Measured: a custom mapping with ``keys()``
    and ``__getitem__`` whose ``__class__`` raises is coerced and
    persisted perfectly well by the runner's ``dict(result.structured or
    {})`` — production completes the run — while a bare ``isinstance``
    in the battery escaped ``run_battery`` with a traceback.

    Unaskable counts as **False**, which is what a declared-type check
    should conclude: the value is not confirmed to be the declared type.
    The failure text renders the class through ``_safe_type_name``,
    which uses ``type()`` rather than ``__class__`` and so is not fooled
    by the same lie.
    """
    try:
        return isinstance(value, types)
    except Exception:
        return False


def _safe_type_name(value: Any) -> str:
    """The class name of a value, when even that may not render.

    ``type(x).__name__`` looks like the one thing that always works, so
    it is what every fallback below reaches for — which makes it the
    last place that may not raise. A metaclass can define ``__name__``
    as a property, and an agent's own exception classes are the most
    likely place to find one, so the ultimate fallback names nothing at
    all rather than trusting the name.
    """
    try:
        name = type(value).__name__
    except Exception:
        return "?"
    # `type(name) is str`, not `isinstance`: a metaclass may return a
    # `str` SUBCLASS, and this value is interpolated into f-strings by
    # every caller — which runs the subclass's `__format__`. The floor
    # under every other guard has to be exact, not merely string-ish.
    return name if type(name) is str else "?"


def _safe_repr(value: Any) -> str:
    """``repr`` that cannot itself raise, for ANY reason.

    Every diagnostic in this module interpolates the offending value —
    that is what makes a failure actionable rather than "serialization
    failed". But rendering a value runs the value's own code, and the
    value came from the agent under test. Two separate ways that bites:

      * ``repr`` of an oversized integer raises the very ``ValueError``
        being reported (CPython caps int-to-string at 4300 digits), so
        the report becomes the crash. Measured: ``str``, ``repr``,
        ``!r``, plain f-string interpolation and ``int.__repr__`` all
        raise on ``10**5000``; only hex formatting and ``bit_length()``
        survive.
      * a user-defined ``__repr__`` raises whatever it likes —
        ``RuntimeError``, ``TypeError``, an exception of its own — or
        returns a non-string, which makes the ``repr`` builtin raise
        ``TypeError``. Catching only ``ValueError`` fixed the first
        case and left the second, which is why this is ``Exception``.

    Every fallback here is itself guarded: ``bit_length`` is overridable
    on an ``int`` subclass, and the class name has its own helper.
    """
    try:
        rendered = repr(value)
    except Exception:
        rendered = None
    if rendered is not None:
        # `repr()` guarantees a `str` INSTANCE, which includes a
        # subclass — and every caller drops this return value into an
        # f-string, running that subclass's `__format__`. Normalizing
        # only the fallback path (as the previous round did) left the
        # SUCCESS path handing the hazard straight back out.
        exact = _as_exact_str(rendered)
        if exact is not None:
            return exact
    if isinstance(value, int):
        try:
            return f"<int {value.bit_length()} bits, 0x{value:x}>"
        except Exception:
            pass
    return f"<unrepresentable {_safe_type_name(value)}>"


def _safe_str(value: Any) -> str:
    """``str`` that cannot itself raise — the ``repr``-free twin.

    Exception messages are interpolated as ``{exc}``, not ``{exc!r}``,
    and an agent chooses both the exception type and its arguments:
    ``raise RuntimeError(10**5000)`` makes ``str(exc)`` raise, so the
    battery crashes reporting the crash.
    """
    try:
        rendered = _exact_str(value) if isinstance(value, str) else str(value)
    except Exception:
        rendered = None
    if rendered is not None:
        # `str()` has the same hole as `repr()`: it accepts a `str`
        # SUBCLASS back from `__str__` and hands it on.
        exact = _as_exact_str(rendered)
        if exact is not None:
            return exact
    if isinstance(value, int):
        try:
            return f"<int {value.bit_length()} bits, 0x{value:x}>"
        except Exception:
            pass
    return f"<unrenderable {_safe_type_name(value)}>"


def _exc_text(exc: BaseException) -> str:
    """``Type: message`` for an exception raised by code under test.

    Both halves come from the agent, so both go through a guard — the
    type name included, since that is exactly the part every other
    fallback assumes is safe.

    The arguments are the fallback rather than a bare "unrenderable"
    because ``str(exc)`` is ``str(args[0])`` for the single-argument
    case: an agent raising ``RuntimeError(10**5000)`` defeats ``str``
    while the argument itself is still perfectly describable. Naming it
    keeps the report pointing at what the agent actually raised, which
    is the entire reason these diagnostics interpolate values at all.
    """
    name = _safe_type_name(exc)
    try:
        return f"{name}: {exc}"
    except Exception:
        pass
    try:
        args = tuple(exc.args)
    except Exception:
        args = ()
    if args:
        return f"{name}: " + ", ".join(_safe_repr(a) for a in args)
    return f"{name}: <unrenderable {name}>"


def _is_empty(value: Any) -> bool:
    """``not value`` for a value whose ``__bool__`` is the agent's code.

    The emptiness checks below run on ``out.report_html``, which reaches
    them as whatever the agent put there: the type check that rejects a
    non-string only records a failure and sets ``phase_fatal``, which is
    not read until well after these lines. So an object with a raising
    ``__bool__`` is truth-tested here, and the battery dies on it.

    Answers "not empty" when the question cannot be asked. The check is
    *did the agent produce nothing*, and an object that exists but
    refuses to be truth-tested is not nothing — it is a bad value, which
    the type check above has already reported under its own name.
    """
    try:
        return not value
    except Exception:
        return False


# The runner's result-attribute accesses **in order**, transcribed from
# agent_runner.py rather than summarized. Line numbers are the reads
# themselves, so the next reader can diff this against the runner.
#
# Counting each attribute separately was still an approximation, and a
# detectable one: production INTERLEAVES these, and result properties
# can share state, so `status, status, status, report_html,
# report_html` is a different experiment from what the runner runs. A
# `status` that raises once a `report_html` counter reaches two passes
# the grouped version and errors the real run.
#
# The FIRST access of each attribute is the one the outcome hangs on —
# `ok` at :495 and the persisted `snap.report_html` at :499 — while the
# later ones feed the span attribute and the completion log.
# Each entry is (attribute, agent_runner.py line, operation) — the
# runner's step at that position, not merely its `getattr`.
#
# The operation matters as much as the read. Production tests
# membership at :495 BEFORE it reads `report_html` at :499, so a
# `__hash__` with side effects that `report_html` observes puts the two
# in a different state than a replay that fetches every attribute first
# and tests afterwards. Reading in order was still only half of "in
# order".
_RUNNER_STEPS_FINAL = (
    ("structured", 493, "coerce"),      # dict(result.structured or {})
    (None, 494, "drop_drifts"),         # structured.pop("_drifts", None)
    ("status", 495, "membership"),      # ok = ... in _SUCCESS_STATUSES
    ("report_html", 499, None),         # snap.report_html = ...
    (None, 509, "persist"),             # await db.commit() -> JSONB encode
    ("status", 516, None),              # span attribute
    (None, 517, "sort_keys"),           # sorted(list(structured.keys()))
    ("report_html", 519, "length"),     # len(... or "") for report_chars
    ("status", 537, None),              # completion log
)
_RUNNER_STEPS_NON_FINAL = (
    ("structured", 493, "coerce"),
    (None, 494, "drop_drifts"),
    ("status", 495, "membership"),
    (None, 509, "persist"),             # await db.commit() -> JSONB encode
    ("status", 529, None),              # span attribute
    ("display", 530, None),
    ("status", 537, None),
)

# Why each attribute matters, for the diagnostic when a step fails.
_RUNNER_READS = {
    "structured": "the runner's first act is `dict(result.structured or {})` "
                  "(agent_runner.py:493)",
    "status": "the runner reads it three times — the success test "
              "(agent_runner.py:495), the span attribute (:516/:529) and the "
              "completion log (:537)",
    "report_html": "the final phase persists it (agent_runner.py:499) and then "
                   "measures its length (:519) — two separate reads",
    "display": "a non-final phase puts it on the span (agent_runner.py:530)",
}

# What each operation is, in the diagnostic's words.
_RUNNER_OPS = {
    "coerce": "`dict(result.structured or {})`",
    "drop_drifts": '`structured.pop("_drifts", None)`',
    "membership": "`result.status in _SUCCESS_STATUSES`",
    "length": '`len(result.report_html or "")`',
    "persist": "`await db.commit()` encoding the result into JSONB",
    "sort_keys": "`sorted(list(structured.keys()))` for the span attribute",
}


class _RunnerReplay:
    """What happened when the runner's steps were replayed on a result.

    ``ok`` is the membership verdict from :495 — the single test
    production performs, so the battery performs it once too, at the
    position production performs it, and reuses the answer.
    """

    def __init__(self) -> None:
        self.reads: dict[str, list] = {}
        self.ok: bool | None = None
        self.coerced: dict | None = None
        self.failure: tuple | None = None   # (attribute, line, what, exception)
        # The commit at :509 raising is FATAL but is reported separately:
        # `_json_problem` names the offending path (`structured.ids`) far
        # better than "the commit failed" can, and putting this in
        # `failure` would replace that message with the blunter one. What
        # it must never do is go unrecorded — see the persist step.
        self.persist_error: BaseException | None = None
        # Whether the commit was actually REACHED and SUCCEEDED. Not the
        # same as `persist_error is None`, which is also true when the
        # replay died before :509 and never committed at all — inferring
        # success from the absence of a failure is the trap this whole
        # module is about.
        self.persisted: bool = False
        # The JSON text the commit actually produced. This — not the live
        # object — is what reached the database, so it is what any later
        # storability verdict has to be about.
        self.serialized: str | None = None


def _replay_runner_steps(out: Any, steps: tuple) -> _RunnerReplay:
    """Replay the runner's reads AND operations, in its order.

    Stops at the first failure, because production stops there: the step
    raises inside ``_run_phases`` and the phase is over. Continuing
    would run an experiment the runner never runs.
    """
    replay = _RunnerReplay()
    for name, line, op in steps:
        value = None
        if name is not None:
            try:
                value = getattr(out, name, None)
            except Exception as exc:
                replay.failure = (name, line, "read", exc)
                return replay
            replay.reads.setdefault(name, []).append(value)
        if op is None:
            continue
        try:
            if op == "coerce":
                replay.coerced = dict(value or {})
            elif op == "drop_drifts":
                # The runner's own line, in its own position. A key whose
                # `__hash__` collides with `"_drifts"` and whose `__eq__`
                # raises makes this `pop` raise — and doing it after the
                # replayed reads rather than between :493 and :495 would
                # also let a mutating `__eq__` be observed in the wrong
                # order by everything downstream.
                if replay.coerced is not None:
                    replay.coerced.pop("_drifts", None)
            elif op == "membership":
                replay.ok = value in RUNNER_SUCCESS_STATUSES
            elif op == "sort_keys":
                # `sorted(list(structured.keys()))` (agent_runner.py:517),
                # BETWEEN the status read at :516 and the report_html read
                # at :519 — and sorting runs the keys' own `__lt__`. A key
                # comparison that mutates state a later `report_html`
                # property observes was being run by the battery much
                # later, inside `_json_problem`, so the two reads happened
                # in an order production never produces.
                if replay.coerced is not None:
                    sorted(list(replay.coerced.keys()))
            elif op == "length":
                len(value or "")
            elif op == "persist":
                # `await db.commit()` (agent_runner.py:509) serializes the
                # coerced dict into JSONB, which ITERATES the agent's own
                # containers — and it sits BETWEEN the reads before it and
                # the reads after it. That position is the whole reason it
                # belongs in the table: a container whose iteration flips a
                # flag that a later `status` read observes was being run by
                # the battery only after every status read had happened.
                #
                # A FATAL replay failure when it raises, via the handler
                # below — not swallowed.
                #
                # It was swallowed at first, on the reasoning that
                # `_json_problem` would rediscover the same failure with a
                # better path. That reasoning is wrong for exactly the
                # objects this replay exists for: a container that raises
                # only on its FIRST serialization lets the later walk
                # succeed, `readable` stays true, and the battery reports
                # PASS while production stopped at the failed commit and
                # marked the run errored. "Something else will notice"
                # is not a guarantee when the thing being asked twice is
                # free to answer differently.
                if replay.coerced is not None:
                    try:
                        serialized = json.dumps(replay.coerced)
                    except Exception as persist_exc:
                        replay.persist_error = persist_exc
                        return replay
                    replay.persisted = True
                    replay.serialized = serialized
        except Exception as exc:
            replay.failure = (name or "structured", line, op, exc)
            return replay
    return replay


def _reads_diverged(reads: list) -> bool:
    """Did the runner's repeated accesses see different values?

    Guarded, because ``__eq__`` belongs to the agent as much as
    ``__str__`` does — and an identity check first, so a value that
    refuses comparison but IS the same object is not reported.
    """
    if len(reads) < 2:
        return False
    first = reads[0]
    for other in reads[1:]:
        if first is other:
            continue
        try:
            if first != other:
                return True
        except Exception:
            return True
    return False


def _attr_of(obj: Any, name: str) -> Any:
    """``getattr(obj, name, None)`` when the attribute may be a property.

    ``getattr``'s default only swallows ``AttributeError``; a property
    raising anything else propagates. Every attribute read this way —
    ``status``, ``error``, ``report_html``, ``structured`` — belongs to
    a result object the AGENT defined, so any of them can be a property
    that raises, and the battery would die reading the value it exists
    to report on.
    """
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _path_of(path: str, key: Any) -> str:
    """Extend a diagnostic path with a key that may not be renderable."""
    return f"{path}.{_exact_str(key) if isinstance(key, str) else _safe_repr(key)}"


def _as_exact_str(value: Any) -> str | None:
    """The exact ``str`` behind a value, or ``None`` if there isn't one.

    ``isinstance(x, str)`` is this module's licence to call string
    methods, and it covers two very different things:

      * a genuine ``str`` **subclass**, which owns ``encode``,
        ``__contains__`` and ``__format__`` but really does carry string
        data. Normalizing it is lossless and correct.
      * an object whose ``__class__`` property merely *says* ``str``.
        That is not a string at all — ``json.dumps`` refuses it and the
        chassis' ``len(... or "")`` raises on it.

    Returning ``None`` for the second is the whole point of this
    function existing separately from ``_exact_str``. Collapsing them
    was a real defect: substituting a plausible-looking ``repr`` made
    the text checks report "no problem" while the ORIGINAL object stayed
    in the result, so the battery certified output production then died
    on. **A coercion that hides a type failure is worse than the crash
    it replaced.**

    Measured — only two primitives yield an exact ``str`` without
    running a line of subclass code::

        str.__str__(v)                    -> str   OK
        str.__getitem__(v, slice(None))   -> str   OK
        str(v), v[:], "" + v, "%s" % v, f"{v}"     all run the override
    """
    if type(value) is str:
        return value
    try:
        return str.__str__(value)
    except Exception:
        return None


def _exact_str(text: Any) -> str:
    """``_as_exact_str`` for the places that only need to RENDER.

    Diagnostics have to say something, so a value with no string behind
    it degrades to its ``repr`` here. Callers deciding whether a value
    is *acceptable* must use ``_as_exact_str`` and treat ``None`` as the
    type failure it is.

    The ``repr`` is normalized in turn, because ``repr()`` accepts a
    ``str`` **subclass** as a return value — measured, not assumed — so
    the fallback of the guard was reintroducing exactly what the guard
    exists to remove.
    """
    exact = _as_exact_str(text)
    if exact is not None:
        return exact
    # `_safe_repr` is exact by construction now — it normalizes its own
    # success path — so this needs no second pass.
    return _safe_repr(text)


def _pg_text_problem(text: str) -> str | None:
    """Why PostgreSQL could not store this string, or ``None`` if it can.

    ``json.dumps`` succeeding is not the same question as "can the
    database hold it", which is the distinction this whole battery
    exists on. Measured against a live PostgreSQL 16 rather than
    inferred — the full sweep, not just the reported character:

    ==================  ==========  =========================  ==========
    character           json.dumps  jsonb                      text column
    ==================  ==========  =========================  ==========
    ``chr(0)`` NUL      ok          UntranslatableCharacter    rejected
    unpaired surrogate  ok          InvalidTextRepresentation  rejected
    ``chr(1)``/``chr(7)``  ok       ok                         ok
    ``chr(127)`` DEL    ok          ok                         ok
    astral emoji        ok          ok                         ok
    newline             ok          ok                         ok
    ==================  ==========  =========================  ==========

    So exactly two things are unstorable, and both are rejected by the
    ``Text`` column as well as by JSONB — which is why ``report_html``
    is checked with this too, not only ``structured``. PostgreSQL's own
    words for the NUL case: ``\\u0000 cannot be converted to text``.

    Normalized on entry: ``in`` and ``encode`` below are both overridable
    by a ``str`` subclass, and this is where the battery invokes them.

    A value with no string behind it at all — ``isinstance`` says yes
    because ``__class__`` lies — is REPORTED rather than normalized.
    Substituting its ``repr`` and answering "storable" was worse than
    the crash it replaced: the original object stays in the result, so
    the battery blessed a ``report_html`` that production then calls
    ``len(... or "")`` on (agent_runner.py:518) and dies.
    """
    exact = _as_exact_str(text)
    if exact is None:
        return (
            f"claims to be a str (isinstance passes) but has no string "
            f"behind it — `str.__str__` refuses it, `json.dumps` refuses "
            f"it, and the chassis' own `len(... or \"\")` raises on it"
        )
    text = exact
    if "\x00" in text:
        return (
            "contains a NUL character — json.dumps writes it as \\u0000 and "
            "PostgreSQL refuses that escape (\\u0000 cannot be converted to text)"
        )
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return (
            "contains an unpaired surrogate — json.dumps emits it happily, but "
            "it is not valid UTF-8 and PostgreSQL rejects it"
        )
    return None


def _pg_text_problem_deep(value: Any, path: str) -> str | None:
    """Scan a whole subtree for strings PostgreSQL cannot store.

    Iterative and cycle-safe rather than recursive, because this runs
    exactly where the recursion cutoff gave up — so it cannot itself
    rely on depth. The ``seen`` set is keyed on ``id()`` so a
    self-referential state terminates instead of spinning.

    This exists because the cutoff's ``json.dumps`` fallback answers a
    different question from the one that matters: it succeeds for NUL
    and for unpaired surrogates, both of which PostgreSQL refuses.
    """
    stack: list[tuple[Any, str]] = [(value, path)]
    seen: set[int] = set()
    while stack:
        item, item_path = stack.pop()
        if isinstance(item, str):
            problem = _pg_text_problem(item)
            if problem:
                return f"{item_path}: {problem}"
            continue
        if id(item) in seen:
            continue
        if isinstance(item, dict):
            seen.add(id(item))
            for key, sub in item.items():
                if isinstance(key, str):
                    problem = _pg_text_problem(key)
                    if problem:
                        return f"{item_path}: key {_safe_repr(key)} {problem}"
                stack.append((sub, _path_of(item_path, key)))
        elif isinstance(item, (list, tuple)):
            seen.add(id(item))
            for index, sub in enumerate(item):
                stack.append((sub, f"{item_path}[{index}]"))
    return None


def _json_problem_walk(
    value: Any,
    path: str = "structured",
    depth: int = 0,
    *,
    check_top_level_order: bool = True,
) -> str | None:
    """Describe the first value in ``value`` that JSONB could not store.

    The `isinstance(x, dict)` checks above only inspect the TOP level, so
    an adapter returning ``{"ids": {1, 2}}`` or a bare ``UUID`` inside an
    otherwise ordinary dict passes them and dies at COMMIT — after the
    agent has done all its work. The chassis persists this through
    SQLAlchemy's default ``json.dumps`` into a JSONB column, so the
    battery runs the same encode and fails conformance instead.

    ``allow_nan=False`` on purpose: ``json.dumps`` happily emits bare
    ``NaN``/``Infinity``, which are not JSON and which Postgres rejects
    on the way into JSONB. Permitting them here would certify output the
    database refuses.

    The structure is walked FIRST and encodability checked at the leaves,
    rather than "try to encode, and only investigate if that fails".
    That order matters because ``json.dumps`` is lenient exactly where
    the chassis is strict: it silently coerces non-string mapping keys,
    so an encode-first check returns success — while the runner then
    calls ``sorted(structured.keys())`` and raises.

    **The chassis' two operations have different reaches, and conflating
    them made this check over-strict.** Measured, not assumed:

    * ``{1: "a", 2: "b"}`` — survives. ``sorted`` compares ints fine and
      ``dumps`` coerces them to ``"1"``/``"2"``.
    * ``{1: "a", "b": 2}`` — **dies**. ``sorted`` raises on mixed types.
    * ``{"items": {1: "ok"}}`` — survives. Nested keys are never sorted;
      ``dumps`` writes ``{"items": {"1": "ok"}}``.
    * ``{"items": {(1, 2): "ok"}}`` — **dies**. ``dumps`` refuses a tuple
      key at any depth.

    So ``sorted(structured.keys())`` (agent_runner.py:517) touches the
    **top level only**, while ``json.dumps`` reaches every depth and
    accepts str/int/float/bool/None keys anywhere. This walk previously
    rejected any non-string key at any depth, which failed a
    production-compatible adapter and — worse — set ``phase_fatal``, so
    its later phases were never exercised either.

    Naming the offending path is the other reason to walk: "serialization
    failed" is not actionable and ``structured.nested.who`` is.
    """
    if depth >= _MAX_WALK_DEPTH:
        # Depth backstop for a self-referential state. Fall back to a
        # whole-subtree encode — which also catches circular references,
        # since json raises ValueError on those.
        #
        # But `json.dumps` cannot answer the PostgreSQL question: it
        # succeeds for both NUL and unpaired surrogates. So the string
        # check has to survive the cutoff, or output deep enough to
        # reach it passes the battery and fails at COMMIT — the exact
        # defect this cutoff was hiding.
        deep = _pg_text_problem_deep(value, path)
        if deep:
            return deep
        try:
            json.dumps(value, allow_nan=False)
            return None
        except Exception as exc:
            # `json.dumps` runs agent code on the way down: a real `dict`
            # subclass is encoded through `PyMapping_Items`, a real `list`
            # subclass through its `__iter__`. Either can raise anything,
            # so TypeError/ValueError is not the whole failure set.
            return f"{path}: {_exc_text(exc)}"

    if isinstance(value, dict):
        if depth == 0 and check_top_level_order:
            # Retained for callers that ask for it explicitly, but the
            # per-phase inspection no longer does: the replay performs
            # `sorted(list(structured.keys()))` at :517, where production
            # performs it, between the status read at :516 and the
            # report_html read at :519.
            #
            # Sorting here as WELL would be the battery running an
            # operation production runs once — on keys whose `__lt__` is
            # the agent's code — so the second comparison could put a
            # value into a state the real run never reaches. The fidelity
            # rule that added the replay applies to the walk too.
            # The runner's own expression, run rather than described, so
            # the two cannot disagree about which key sets are orderable.
            # Uniform non-string keys sort fine and are NOT a failure.
            #
            # Only reached for the FINAL phase, because that is the only
            # place the runner sorts: `sorted(list(structured.keys()))`
            # sits inside `if is_final:` (agent_runner.py:517), while a
            # non-final result goes to `snap.analysis` and is committed
            # unsorted. And the `safe_json(...)` around it is no help —
            # the dict literal is built, so `sorted` raises, before
            # safe_json is ever called.
            try:
                sorted(value.keys())
            except Exception as exc:
                # `except Exception`, not `except TypeError`. TypeError is
                # what an unorderable key SET raises, and it was the only
                # failure this guard imagined — but sorting runs the keys'
                # own `__lt__`, and a real `str` subclass can raise
                # anything from it. The runner hits the same expression
                # AFTER `await db.commit()`, so an escape here is a crash
                # with the result already stored.
                return (
                    f"{path}: the chassis calls sorted(structured.keys()) "
                    f"(agent_runner.py:517) on the final phase's output, which "
                    f"raises on these keys — {_exc_text(exc)}"
                )
        for key, item in value.items():
            # What `json.dumps` accepts as an object key at ANY depth.
            # These it coerces to strings; anything else it refuses
            # outright, which is a failed run at COMMIT.
            if not (key is None or isinstance(key, (str, int, float, bool))):
                return (
                    f"{path}: key {_safe_repr(key)} is {_safe_type_name(key)} — json.dumps "
                    f"accepts only str/int/float/bool/None as object keys, so "
                    f"the chassis cannot persist this into JSONB"
                )
            if isinstance(key, str):
                text_problem = _pg_text_problem(key)
                if text_problem:
                    return f"{path}: key {_safe_repr(key)} {text_problem}"
            elif isinstance(key, int) and not isinstance(key, bool):
                # An oversized int key passes the type check above and
                # then dies in the encoder: CPython caps int-to-string
                # at 4300 digits, so `json.dumps` raises rather than
                # coercing. Probed with the encoder rather than by
                # comparing digit counts, so the battery agrees with
                # whatever limit the interpreter is actually running.
                try:
                    json.dumps({key: 0})
                except Exception as exc:
                    # Building the probe dict hashes the key, and an
                    # `int` subclass owns `__hash__`. It hashed once to
                    # get into the agent's own dict, which does not
                    # oblige it to hash again.
                    return (
                        f"{path}: key {_safe_repr(key)} cannot be encoded — "
                        f"{_exc_text(exc)}"
                    )
            problem = _json_problem(item, _path_of(path, key), depth + 1)
            if problem:
                return problem
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problem = _json_problem(item, f"{path}[{index}]", depth + 1)
            if problem:
                return problem
        return None

    if isinstance(value, str):
        # The leaf case the encode below cannot answer: `json.dumps`
        # accepts NULs and unpaired surrogates, PostgreSQL does not.
        text_problem = _pg_text_problem(value)
        if text_problem:
            return f"{path}: {text_problem}"

    try:
        json.dumps(value, allow_nan=False)
    except Exception as exc:
        # Same reason as the depth backstop: a value is a "leaf" here
        # only because `isinstance` said so, and `isinstance` consults
        # `__class__`. What `json.dumps` then does with the real object
        # is not bounded by TypeError/ValueError.
        return f"{path}: {_exc_text(exc)}"
    return None


class _Uninspectable(str):
    """A problem string produced by a walk FAILING, not by a real defect.

    The distinction matters in one direction only. When production's
    commit succeeded, anything the battery's extra traversals then choke
    on is an artifact of asking a second time — production asks once at
    :509 and afterwards sorts only the TOP-LEVEL keys (:517), so it never
    performs the nested re-traversal at all.

    A subclass rather than a sniffed prefix: every caller keeps treating
    it as the string it is, and the one place that needs to tell them
    apart asks with `isinstance` instead of matching on wording that a
    later edit would quietly break.
    """


def _json_problem(
    value: Any,
    path: str = "structured",
    depth: int = 0,
    *,
    check_top_level_order: bool = True,
) -> str | None:
    """``_json_problem_walk`` with a boundary around it.

    Every step of the walk is an operation on an agent-supplied object,
    and any of them can raise anything: ``value.keys()`` and
    ``value.items()`` on a real ``dict`` subclass, ``enumerate(value)``
    on a real ``list`` subclass, ``hash(key)`` when the probe dict is
    built, ``sorted`` running the keys' ``__lt__``, and ``json.dumps``
    calling into all of those itself. Guarding them one at a time is how
    the last several rounds went; the branch nobody thought of is always
    the next one.

    So the walk gets ONE boundary instead. There is no enclosing ``try``
    between here and ``run_battery``'s signature — measured, not assumed
    — so before this, an escape came back to the adapter author as a
    traceback out of the battery rather than as a failed ``BatteryResult``
    naming the agent's own defect.

    The recursion goes through this wrapper rather than around it, so a
    failure is caught at the deepest level that saw it and keeps the
    precise path: ``structured.rows[3]`` and not ``structured``.

    An un-inspectable value is a FAILURE, never a pass. The chassis runs
    these same operations on the way into JSONB, so "the battery could
    not look at this" and "the run dies at COMMIT" are the same finding.
    """
    try:
        return _json_problem_walk(
            value, path, depth, check_top_level_order=check_top_level_order
        )
    except Exception as exc:
        return _Uninspectable(
            f"{path}: could not be inspected — {_exc_text(exc)}. The chassis "
            f"performs these same operations to persist the result, so this "
            f"raises there too, after the phase has done its work"
        )


def _json_key_name(key: Any) -> str:
    """The string ``json.dumps`` will actually write for this object key.

    Mirrors CPython's encoder (``json/encoder.py``) rather than
    approximating it with ``str()``, because the two disagree exactly
    where a collision check needs them to agree::

        str(float("nan"))            -> 'nan'
        json.dumps({float("nan"): 0}) -> '{"NaN": 0}'

    So ``{float("nan"): "x", "NaN": "y"}`` collides in production —
    it round-trips to ``{"NaN": "y"}`` and loses a value — while a
    ``str()``-derived comparison sees two different names and reports
    nothing. Same for ``inf``/``-inf``, which JSON spells ``Infinity``
    and ``-Infinity``.

    The int branch matters too: ``int.__repr__`` is what json uses, so
    an ``IntEnum`` key is named by its VALUE here, as production writes
    it, not by ``str()``'s member name.
    """
    if isinstance(key, str):
        # Exact, not as-is: this name is compared, sorted and formatted
        # downstream, and a subclass owns every one of those operations.
        return _exact_str(key)
    if isinstance(key, float):
        # json's own `floatstr`, non-finite spellings included.
        if key != key:
            return "NaN"
        if key == float("inf"):
            return "Infinity"
        if key == float("-inf"):
            return "-Infinity"
        return float.__repr__(key)
    # Before the int branch: `isinstance(True, int)` is True, and json
    # spells booleans as `true`/`false`, not `1`/`0`.
    if key is True:
        return "true"
    if key is False:
        return "false"
    if key is None:
        return "null"
    if isinstance(key, int):
        # `int.__repr__` specifically, NOT `_safe_repr`: json uses the
        # numeric repr, so an `IntEnum` key must be named by its VALUE
        # ("200"), where `repr` would give "<Code.OK: 200>". Guarded
        # only for the oversized case, where json itself refuses the
        # key — `_json_problem` reports that as unstorable, so the name
        # produced here is never a real JSON key and exists purely so
        # this function stays total.
        try:
            return int.__repr__(key)
        except Exception:
            # Same text as the hand-written fallback this replaces —
            # `_safe_repr` reaches its own int branch here — but total,
            # where `bit_length` and `__format__` are both overridable
            # on an `int` subclass.
            return _safe_repr(key)
    # Unreachable for keys `_json_problem` admits — it rejects anything
    # json.dumps would refuse outright — but a total function is easier
    # to reason about than one with a hole in it. Which this line was
    # NOT: a bare `str()` on an arbitrary key runs the key's own
    # `__str__`, so the total function had a hole in exactly the place
    # its comment promised there wasn't one.
    return _safe_str(key)


def _json_lossy_deep(value: Any, path: str) -> str | None:
    """Collision scan that survives the recursion cutoff.

    Iterative and ``id()``-keyed for the same reason as
    ``_pg_text_problem_deep``: it runs exactly where the recursion gave
    up, so it cannot rely on depth, and the self-referential state the
    cutoff exists for must still terminate.
    """
    stack: list[tuple[Any, str]] = [(value, path)]
    seen: set[int] = set()
    while stack:
        item, item_path = stack.pop()
        if id(item) in seen:
            continue
        if isinstance(item, dict):
            seen.add(id(item))
            names: dict[str, Any] = {}
            for key in item:
                name = _json_key_name(key)
                if name in names:
                    return (
                        f"{item_path}: keys {_safe_repr(names[name])} and {_safe_repr(key)} both "
                        f'encode to "{name}" — JSON has one overwrite the '
                        f"other, so the chassis persists a value the agent "
                        f"did not produce"
                    )
                names[name] = key
            for key, sub in item.items():
                stack.append((sub, _path_of(item_path, key)))
        elif isinstance(item, (list, tuple)):
            seen.add(id(item))
            for index, sub in enumerate(item):
                stack.append((sub, f"{item_path}[{index}]"))
    return None


def _json_lossy(value: Any, path: str = "structured", depth: int = 0) -> str | None:
    """Describe a key collision that JSON encoding would silently resolve.

    Separate from ``_json_problem`` because the two thresholds differ:
    this one production *survives*. ``{1: "x", "1": "y"}`` encodes without
    complaint and round-trips to ``{"1": "y"}`` — the run completes, and
    a value the agent produced is simply gone. Measured, not reasoned::

        >>> json.loads(json.dumps({"a": {1: "x", "1": "y"}}))
        {'a': {'1': 'y'}}

    Nothing raises, so aborting the phase would make the battery stricter
    than the chassis — the mistake this same walk was just corrected for.
    It is reported instead, because silently losing a value is exactly
    what an adapter author should hear about, and because the LangGraph
    adapter's own ``_json_safe`` already disambiguates such keys rather
    than dropping one. A battery that failed to notice what its first
    adapter takes care to prevent would be certifying the absence of a
    property it never checked.
    """
    if depth >= _MAX_WALK_DEPTH:
        # NOT a silent stop. `_json_problem` accepts a deep subtree, so
        # returning None here means a collision below the cutoff is
        # certified while production still collapses the two keys and
        # loses a value — the very failure this walk exists to catch,
        # reintroduced by the depth guard. Same miss as the PostgreSQL
        # check had one function above, which is why both now hand off
        # to a cycle-safe iterative scan instead of giving up.
        return _json_lossy_deep(value, path)
    if isinstance(value, dict):
        seen: dict[str, Any] = {}
        for key in value:
            name = _json_key_name(key)
            if name in seen:
                return (
                    f"{path}: keys {_safe_repr(seen[name])} and {_safe_repr(key)} both encode to "
                    f'"{name}" — JSON has one overwrite the other, so the '
                    f"chassis persists a value the agent did not produce"
                )
            seen[name] = key
        for key, item in value.items():
            problem = _json_lossy(item, _path_of(path, key), depth + 1)
            if problem:
                return problem
        return None
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            problem = _json_lossy(item, f"{path}[{index}]", depth + 1)
            if problem:
                return problem
    return None


def _path_to(value: Any, target: Any, path: str = "structured") -> str | None:
    """Where does ``target`` sit inside a value ``json.loads`` produced?

    Identity, not equality — ``==`` would match the first structurally
    equal twin and report the wrong path.

    No cycle guard and a plain ``isinstance``, both honest here and
    nowhere else in this file: every container in a parser's output is
    freshly built and referenced once, so the result is a tree, and none
    of it is agent-owned. This walk must never be pointed at a live
    agent value.
    """
    stack: list[tuple[Any, str]] = [(value, path)]
    while stack:
        item, item_path = stack.pop()
        if item is target:
            return item_path
        if isinstance(item, dict):
            for key, sub in item.items():
                stack.append((sub, _path_of(item_path, key)))
        elif isinstance(item, list):
            for index, sub in enumerate(item):
                stack.append((sub, f"{item_path}[{index}]"))
    return None


def _committed_json(text: str) -> tuple[Any, str | None] | None:
    """Parse the committed text, and find a collision INSIDE that text.

    Returns ``(value, lossy, dropped)``, or ``None`` when the text will
    not parse. ``lossy`` describes a duplicate name (survivable);
    ``dropped`` describes a value the duplicate threw away that the
    database will choke on anyway (fatal).

    A plain ``json.loads`` is no good for the collision question, because
    it has already resolved it: the duplicate name is gone from the
    result, which is precisely the data loss being looked for. Measured::

        >>> json.dumps({"nested": _collides_once()})
        '{"nested": {"1": "first", "1": "second"}}'
        >>> json.loads('{"nested": {"1": "first", "1": "second"}}')
        {'nested': {'1': 'second'}}

    So the pairs are read as the parser sees them, before it collapses
    them. ``object_pairs_hook`` returns ``dict(pairs)`` — exactly what
    the default parse would have produced — so ONE parse answers both
    questions and nothing is asked twice.

    The alternative, walking the live object again, is what this
    replaced: a dict subclass whose first ``items()`` yields colliding
    pairs and whose later calls yield safe ones commits a payload that
    loses a value, then shows a clean face to any later traversal.

    The DISCARDED values are kept too, and this is the sharper edge of
    the same problem. Collapsing a duplicate name throws away a value,
    and that value is part of the committed text whether or not it wins
    the name::

        >>> json.loads('{"x": NaN, "x": 1}')
        {'x': 1}

    Nothing left in that result is unstorable, and PostgreSQL still
    refuses the commit — it parses the whole document, and the token it
    rejects does not have to be the one that survives. Reporting only
    the (survivable) collision would let the battery run later phases
    while the real run stopped dead at COMMIT.
    """
    collisions: list[tuple[Any, str, Any, Any]] = []
    discarded: list[tuple[Any, str, Any]] = []

    def hook(pairs):
        # `pairs` is a list of (name, value) the parser read in order.
        # Built before the scan so the object is retained: `_path_to`
        # compares by identity, and an object the parse discards (a
        # duplicate name at the level ABOVE) could otherwise have its
        # id reused by a later allocation.
        obj = dict(pairs)
        seen: dict[str, Any] = {}
        for name, item in pairs:
            if name in seen:
                if not collisions:
                    collisions.append((obj, name, seen[name], item))
                # Every earlier value under a repeated name, not just
                # the first: `dict(pairs)` keeps the LAST, so with three
                # occurrences two are thrown away and either can be the
                # token the database rejects. No `break` for the same
                # reason — stopping at the first collision would stop
                # collecting them.
                discarded.append((obj, name, seen[name]))
            seen[name] = item
        return obj

    try:
        value = json.loads(text, object_pairs_hook=hook)
    except Exception:
        # Unparseable — including a RecursionError from nesting deeper
        # than the parser goes. The caller falls back to the live
        # object, which is all there is when there is no readable
        # commit.
        return None
    if not collisions:
        return value, None, None
    return value, _collision_text(value, collisions[0]), _dropped_text(value, discarded)


def _where(value: Any, obj: Any) -> str:
    """Path to ``obj``, honestly labelled when there is not one.

    An object the parse itself discarded is not IN the result, so
    ``_path_to`` cannot find it. Saying "structured" then would name the
    root as the site of something that is nowhere in it.
    """
    return _path_to(value, obj) or "structured (in a value the committed text discards)"


def _collision_text(value: Any, collision: tuple) -> str | None:
    """The duplicate-name report. Survivable: production completes."""
    try:
        obj, name, first, second = collision
        return (
            f'{_where(value, obj)}: the committed text carries the name '
            f'"{name}" twice ({_safe_repr(first)} then {_safe_repr(second)}) '
            f"— JSON has the later one win, so the chassis persists a value "
            f"the agent did not produce"
        )
    except Exception:
        # The value parsed; only the description failed. Reporting
        # nothing is wrong and raising is worse, so say what is certain.
        return (
            "the committed text carries a duplicate name — JSON has one "
            "value overwrite the other, so the chassis persists a value "
            "the agent did not produce"
        )


def _dropped_text(value: Any, discarded: list) -> str | None:
    """A discarded value the database will refuse. Fatal: the commit dies.

    Only the discarded ones are examined here. Everything that survived
    the collapse is in ``value`` and is checked by ``_json_problem``
    where it sits, so this adds no second traversal of anything — it
    covers exactly the gap the collapse opened.
    """
    for obj, name, dropped in discarded:
        try:
            problem = _json_problem(dropped, check_top_level_order=False)
        except Exception:  # pragma: no cover - `_json_problem` is total
            problem = None
        if problem:
            return (
                f'{_where(value, obj)}: the name "{name}" appears twice and the '
                f"value JSON discards is one the database refuses — {problem}. "
                f"PostgreSQL parses the whole document, so the token it rejects "
                f"does not have to be the one that wins the name"
            )
    return None


def _status_message(value: Any, failure: tuple | None) -> str:
    """Why the runner rejected this status, in the report's words.

    The membership TEST itself is not performed here — the replay does
    it, once, at the position production does it (agent_runner.py:495).
    This only formats the answer, which is what stopped the test being
    run twice with different outcomes.

    Two shapes of rejection:

    * the test raised — an unhashable status like ``["complete"]``, or a
      ``__hash__`` that raises anything else. Production dies there,
      mid-phase, with the phase's work already done. The battery must
      also fail such a result, but by **reporting**: reproducing the
      exception escapes ``run_battery`` and hands an author a traceback
      where they asked for a ``BatteryResult``.
    * the test answered False — an ordinary unsuccessful status.
    """
    if failure is not None and failure[3] is not None and failure[2] == "membership":
        return (
            f"returned a status the runner cannot test ({_safe_repr(value)}) — its "
            f"own membership check (agent_runner.py:495) raises "
            f"{_exc_text(failure[3])} on it, so the run dies mid-phase with the "
            f"phase's work already done"
        )
    return (
        f"returned status {_safe_repr(value)}, which the chassis runner treats as a "
        f"failed run (it accepts {sorted(RUNNER_SUCCESS_STATUSES)})"
    )


def _validate_against_schema(instance: dict, schema: dict) -> str | None:
    """Validate exactly as the intake endpoint does.

    Delegating rather than calling jsonschema directly is the point:
    ``jsonschema.validate`` picks its validator from the schema's
    ``$schema``, so a draft-07 schema would have keywords like
    ``minContains`` ignored here and enforced in production.
    """
    try:
        errors = _chassis_validate(schema, instance)
    except Exception as exc:  # a schema the chassis cannot even compile
        return _exc_text(exc)
    return errors[0] if errors else None


async def run_battery(
    agent: AgentProtocol,
    scenario: Scenario,
    *,
    phases: list[str] | None = None,
    capabilities: object | None = None,
    tenant_id: UUID | None = None,
    run_id: UUID | None = None,
    require_traces: bool = True,
    output_mode: str | None = None,
) -> BatteryResult:
    """Run ``scenario`` through ``agent`` and report on the three promises.

    ``phases`` defaults to a single ``analyze`` phase — the shape most
    adapters start with. Pass the manifest's phase list to exercise a
    multi-phase agent; the battery calls ``run_phase`` exactly as the
    chassis runner does, so an agent that works here works there.

    ``output_mode`` should be your manifest's ``output.mode``. Pass it:
    the two modes render from *different fields*, and the run page
    honours the manifest rather than taking whatever it finds. An agent
    declaring ``structured`` that returns only ``report_html`` produces a
    complete run whose results view says "No structured result
    available." — the HTML is never fetched. Left unset the battery can
    only check that *something* renderable came back, which is the
    weaker question.
    """
    if output_mode is not None and output_mode not in _OUTPUT_MODES:
        raise ValueError(
            f"output_mode must be one of {sorted(_OUTPUT_MODES)}, got {output_mode!r}"
        )
    # Identity is read ONCE per attribute, here, and reused everywhere
    # below — including for `BatteryResult.agent_id`, which previously
    # took its own separate unguarded read. Three problems in one line:
    # a property that raises escaped `run_battery` entirely (an invalid
    # agent is exactly what this API exists to DIAGNOSE, so handing the
    # caller a traceback instead of a failed result is the worst answer
    # available); a stateful property could answer differently between
    # that read and the loop below, so the battery would report on a
    # value it never checked; and a `str` subclass reached the f-strings
    # below unchanged.
    identity: dict[str, Any] = {}
    identity_unreadable: set[str] = set()
    identity_failures: list[str] = []
    for _attr in ("agent_id", "display_name", "description"):
        try:
            identity[_attr] = getattr(agent, _attr, None)
        except Exception as exc:
            identity[_attr] = None
            identity_unreadable.add(_attr)
            identity_failures.append(
                f"agent {_attr} could not be read ({_exc_text(exc)}) — "
                f"AgentProtocol declares it as a plain `str` attribute, and "
                f"discovery reads it while registering the agent"
            )

    _battery_id = identity["agent_id"]
    result = BatteryResult(
        agent_id=_exact_str(_battery_id) if _is_a(_battery_id, str) else "<unset>",
        scenario=scenario.name,
    )
    result.failures.extend(identity_failures)

    # --- identity ---------------------------------------------------
    # Two thresholds here too, and emptiness was only the first. All
    # three are declared `str` on AgentProtocol (protocol.py:88-90), and
    # for `agent_id` production enforces it: discovery compares
    # `instance.agent_id` against the manifest's validated string id and
    # refuses to register on mismatch (registry.py:315), so a non-string
    # id is an agent the picker never offers — the battery was
    # certifying something production would not expose at all.
    #
    # Reported rather than aborting, on the same rule as a missing
    # `description`: the author should still learn whether their
    # progress and tracing work instead of being stopped at the door.
    for attr in ("agent_id", "display_name", "description"):
        if attr in identity_unreadable:
            continue  # already reported as unreadable; nothing to type-check
        value = identity[attr]
        if _is_empty(value):
            result.failures.append(f"agent does not set {attr}")
        elif _as_exact_str(value) is None:
            # `_as_exact_str`, not `isinstance`: an object whose
            # `__class__` says `str` passes `isinstance` with no string
            # behind it, and discovery's comparison against the
            # manifest's validated id (registry.py:315) then fails — so
            # the battery would certify an agent that never registers.
            # Same distinction the storage checks already draw.
            detail = (
                "discovery compares it against the manifest's validated string "
                "id and refuses to register on mismatch, so this agent would "
                "never be offered"
                if attr == "agent_id"
                else "AgentProtocol declares it as str"
            )
            result.failures.append(
                f"agent {attr} is {_safe_type_name(value)}, not a string — {detail}"
            )

    # --- the scenario must satisfy the agent's own input schema ------
    # Tracked separately from `failures`, because not every failure means
    # production would have refused the submission. An agent missing a
    # `description` is a real conformance failure and is reported — but
    # the chassis still runs it, so the battery must too, or the author
    # never learns whether their progress and tracing work.
    intake_blocked = False
    # Whether the LOOKUP itself failed, tracked as its own fact rather
    # than inferred from `result.failures`. The two are not the same
    # question, and using the global list as a proxy for this one is
    # what let a returned `None` slip through unreported whenever the
    # agent already had an unrelated identity failure.
    schema_lookup_failed = False
    try:
        schema = agent.input_schema()
    except NotImplementedError:
        schema = None
        schema_lookup_failed = True
        result.failures.append("agent does not implement input_schema()")
        intake_blocked = True
    except Exception as exc:
        # The intake route wraps this call in a bare `except Exception`
        # and answers 400 (runs.py:145) — a schema that fails to load
        # from config, a bad file path, anything. So the battery must
        # REPORT it and return a BatteryResult, not propagate: a caller
        # asking "is my adapter conformant?" should be told no, not
        # handed the traceback to interpret.
        schema = None
        schema_lookup_failed = True
        result.failures.append(
            f"input_schema() raised {_exc_text(exc)} — the intake "
            f"route catches any exception here and rejects the submission with "
            f"400, so this agent could not accept input"
        )
        intake_blocked = True
    if _is_a(schema, dict):
        problem = _validate_against_schema(scenario.user_inputs, schema)
        if problem:
            result.failures.append(
                f"scenario {scenario.name!r} does not satisfy the agent's own "
                f"input_schema(): {problem}"
            )
            intake_blocked = True
    elif not schema_lookup_failed:
        # Anything RETURNED that is not a dict is a failure, not "no
        # validation to do". The chassis hands this straight to
        # Draft202012Validator, so an agent returning None or a list
        # cannot accept submissions at all — the battery must not pass
        # it quietly.
        #
        # Guarded on the lookup, not on `result.failures`. The old
        # condition asked "has anything failed yet?" as a stand-in for
        # "did the lookup already report itself?", and those diverge the
        # moment an unrelated identity failure is present: `schema=None`
        # with a missing `description` reported neither the invalid
        # schema nor blocked intake, so the battery redacted and ran the
        # agent — paying for model calls and capability side effects on
        # input production rejects before scheduling anything.
        result.failures.append(
            f"input_schema() returned {_safe_type_name(schema)}, not a dict — the "
            f"chassis validates submissions against it, so this agent could "
            f"not accept any input"
        )
        intake_blocked = True

    # A scenario the intake endpoint would reject never becomes a run, so
    # the battery must not invoke the agent on it. Same rule as the
    # redaction abort below: production returns 4xx before scheduling
    # anything, and an adapter that calls a model on impossible input
    # pays for it in tokens and side effects.
    if intake_blocked:
        return result

    # One entry per on_progress CALL, independent of whether the
    # emulated write then succeeded. See the note inside the callback.
    progress_calls: list = []

    async def on_progress(p: StepProgress) -> None:
        # Read each field ONCE, in the order production reads it, and
        # use those values for everything below. The chassis' callback
        # evaluates `p.step_id` as the Redis hash field and then builds
        # the dict literal — `p.status`, then `p.detail`
        # (agent_runner.py:206-210). One read each, in that order.
        #
        # The battery previously read `status` four times and `detail`
        # four times, which is a different experiment: a stateful
        # property returning an unserializable value FIRST and
        # "complete" afterwards was recorded from the first read and
        # then validated and serialized from later ones, so the battery
        # passed while production — which serializes the first value —
        # raises inside the callback and aborts the phase.
        #
        # Unguarded on purpose: production's reads are unguarded too, so
        # a property that raises kills the phase there and must here.
        # Caching is also MORE faithful downstream, not less — redis-py
        # encodes the very object production read, and that is this one.
        # The agent CALLED us. Recorded as the FIRST statement, before the
        # payload is even read, and with no agent code run to record it.
        #
        # The reads below are agent code too: `step_id`, `status` and
        # `detail` can all be properties that raise. Counting after them
        # meant a raising property produced the same false report the
        # counter was added to prevent — "no progress was streamed" about
        # a call the agent plainly made. The invocation is a fact on its
        # own, independent of whether its payload can be read, serialized
        # or written.
        progress_calls.append(None)

        step_id = p.step_id
        status = p.status
        detail = p.detail

        # PRODUCTION'S OPERATIONS FIRST, in production's order, before
        # anything the battery does for its own benefit.
        #
        # The battery's vocabulary test HASHES `status`, and the record
        # it appends REPRs `step_id`. The chassis does neither. Running
        # them first meant a battery-only operation could change the
        # objects before the emulated write touched them: a `str`
        # subclass whose `__hash__` flips a flag that a sibling
        # `encode()` consults would be encoded here in a state
        # production never puts it in — so the battery passed while the
        # real Redis write aborts the phase.
        #
        # Same rule as the runner replay: the emulation may not perform
        # operations production does not, in positions where the code
        # under test can observe them.
        def _record_observations():
            """What the battery saw, and its own diagnosis of the values.

            Runs on BOTH paths — after a successful emulated write, and
            after a failed one just before the failure is re-raised. The
            raised message names the mechanism (redis-py refuses this
            type); these name the declared contract that was broken. An
            author fixing a run wants both, and the first version of this
            reorder dropped the second whenever the write raised.

            The rendered ledger entry lives here for the same reason. It
            runs `repr` on a non-str step_id, which is the battery's own
            operation and must not touch the values before the emulated
            write — but dropping it when the write fails would lose the
            record of an event the agent plainly emitted.
            """
            result.progress.append((_safe_repr(step_id)
                                    if not _is_a(step_id, str)
                                    else _exact_str(step_id), status))
            try:
                status_known = status in VALID_STATUSES
            except Exception:
                # An UNHASHABLE status — `["complete"]` — makes the set
                # membership test itself raise, which would abort the phase
                # from inside the battery's own check. Production doesn't
                # care: `json.dumps` serializes a list happily, stores it,
                # and the run continues. So this is a reported violation, not
                # a stop; the battery must not be the only thing that breaks.
                status_known = False
            if not status_known:
                result.failures.append(
                    f"progress status {_safe_repr(status)} for step {_safe_repr(step_id)} is not one "
                    f"of the chassis vocabulary {sorted(VALID_STATUSES)}"
                )
            if not _is_a(step_id, str):
                # The chassis uses step_id as a Redis hash FIELD
                # (agent_runner.py:207). redis-py encodes bytes/str/int/float
                # and rejects everything else outright — a UUID raises
                # `DataError: Invalid input of type: 'UUID'`.
                result.failures.append(
                    f"progress step_id {_safe_repr(step_id)} is {_safe_type_name(step_id)}, "
                    f"not a string — StepProgress.step_id is declared `str`, and the "
                    f"chassis uses it as a Redis hash field"
                )
            if detail is not None and not _is_a(detail, str):
                # `StepProgress.detail` is declared `str | None`, and the
                # chassis `json.dumps()`es the whole progress record into
                # Redis (agent_runner.py:206) — so a UUID or a model object
                # here raises TypeError and aborts the phase mid-run. The
                # battery recorded the event and dropped the detail, which
                # made the very field that breaks production invisible.
                result.failures.append(
                    f"progress detail for step {_safe_repr(step_id)} is "
                    f"{_safe_type_name(detail)}, not a string — StepProgress.detail "
                    f"is declared `str | None`, and the chassis serializes the "
                    f"progress record, so a non-JSON value aborts the phase"
                )


        try:
            # Recording those failures is not enough: in production the
            # chassis' write happens INSIDE this callback, so a value it
            # cannot handle raises here — inside the agent's own `await
            # on_progress(...)` — killing the phase and everything after it.
            # A battery that noted the problem and returned normally would
            # keep running phases production never reaches, which is the
            # cost-bearing failure mode already fixed for bad statuses and
            # unusable output.
            #
            # Emulated, not described. `json.dumps` is the chassis' own call,
            # so it raises exactly when production would and stays silent for
            # a non-string detail that happens to be serializable — where the
            # declared type is violated but the run survives.
            json.dumps({"status": status, "duration_ms": None, "detail": detail})
            if isinstance(step_id, bool) or not isinstance(
                step_id, (bytes, bytearray, memoryview, str, int, float)
            ):
                # Stands in for redis-py's DataError, which needs a live
                # Redis to raise for real. The type rule is redis-py's own,
                # checked against a live server rather than inferred:
                # bytes/str/int/float encode, `bool` does NOT — redis-py
                # rejects it explicitly before its numeric branch, so the
                # `int` subclassing that makes `True` look acceptable here
                # is exactly the trap. Everything else is refused too.
                raise TypeError(
                    f"step_id {_safe_repr(step_id)} of type {_safe_type_name(step_id)} cannot be "
                    f"a Redis hash field — redis-py raises DataError, aborting the phase"
                )
            # The type allow-list above is only HALF of what redis-py does.
            # Its encoder then runs `value.encode(encoding, errors)` — the
            # string's OWN `encode`, which a `str` subclass owns. Normalizing
            # the copy this battery records changes nothing: production hands
            # `p.step_id` ITSELF to `redis.hset` (agent_runner.py:207-209),
            # so the override runs there and the phase dies.
            #
            # Measured, and the measurement is the point: the SYNC client
            # accepted a hostile subclass, because with `redis[hiredis]`
            # installed it packs commands through `HiredisRespSerializer` in
            # C and never calls `.encode`. The chassis is async, and the
            # async client has no such packer — it goes through the Python
            # encoder and RAISES. Probing the convenient client would have
            # certified a step_id that kills every real run.
            #
            # Run for EVERY accepted type, not just `str`: redis-py encodes
            # an int via `repr(value).encode()`, which an `int` subclass can
            # break just as readily.
            try:
                _redis_encode(step_id)
            except Exception as exc:
                raise TypeError(
                    f"step_id {_safe_repr(step_id)} cannot be encoded for "
                    f"Redis ({_exc_text(exc)}) — redis-py's encoder runs on the "
                    f"object the agent passed, so the chassis' progress write "
                    f"aborts the phase"
                )
        except Exception:
            # Explain, then let production's failure stand.
            _record_observations()
            raise

        # BATTERY-ONLY OBSERVATIONS, after the emulated write.
        #
        # If the write above raised, production's phase is already dead
        # and none of this runs — which is correct. The raised message
        # carries the diagnosis, and a report about vocabulary would be
        # describing a run that no longer exists.
        # `str()` on an oversized-int step_id raises, so the battery would
        # crash while recording the very progress it is measuring.
        _record_observations()
    # --- redact exactly as the intake endpoint does -------------------
    # `POST /runs` validates the raw body, then runs
    # `redact_pii_fields(schema, payload)` before anything is persisted
    # or run (CLAUDE.md: unredacted content never touches the database).
    # So an agent's first sight of an `x-pii` field in production is the
    # REDACTED one, and a battery that skipped this step would exercise
    # input no real run ever produces.
    #
    # The difference is not cosmetic. Presidio rewrites the timestamps in
    # a pasted log as `[REDACTED_DATE_TIME_1]`; a node that keys off them
    # passes conformance on raw input and behaves differently the moment
    # a user submits the same text through the form. Order matters too —
    # validate raw, then redact — because that is the order production
    # uses, and redaction can change a value's length or shape.
    user_inputs = scenario.user_inputs
    if _is_a(schema, dict) and _is_a(user_inputs, dict):
        try:
            user_inputs, result.redactions = _chassis_redact(schema, user_inputs)
        except Exception as exc:
            # Reported AND aborted. Reported because production would 500
            # here, which is a conformance failure worth naming rather
            # than an exception for the caller to untangle — but the
            # abort is the part that matters: `POST /runs` raises before
            # the run is persisted or the runner is scheduled, so the
            # agent never sees the body at all. Recording the failure and
            # carrying on would hand raw `x-pii` values to an agent that
            # may forward them to an external model, which is a leak
            # rather than a failed check.
            result.failures.append(
                f"chassis PII redaction failed on this scenario, so the battery "
                f"stopped before running the agent — production aborts here too, "
                f"and the raw x-pii values must not reach an adapter: "
                f"{_exc_text(exc)}"
            )
            return result

    inp = AgentInput(
        run_id=run_id or uuid4(),
        tenant_id=tenant_id or uuid4(),
        user_inputs=user_inputs,
        capabilities=capabilities,
    )

    # --- run the declared phases the way the chassis does ------------
    with _span_capture() as exporter:
        # A root span for this invocation. Everything the agent opens
        # inherits its trace id (OTEL propagates context through the
        # awaited calls), which is what lets the trace check below
        # distinguish the adapter's spans from a busy process' unrelated
        # traffic.
        from opentelemetry import context as _otel_context
        from opentelemetry import trace as _trace_api

        # Start in an EMPTY context so this really is a new trace. With
        # the ambient context, running the battery inside an
        # instrumented request or an enclosing test span would make this
        # a child — and unrelated siblings under that same parent would
        # share its trace id and satisfy the check below.
        # Used as a real context manager, not entered by hand: an
        # unexpected exception anywhere below must still close it.
        with _trace_api.get_tracer(__name__).start_as_current_span(
            f"adapter_kit:battery:{result.agent_id}",
            context=_otel_context.Context(),
        ) as battery_span:
          battery_trace_id = battery_span.get_span_context().trace_id
          battery_span_id = battery_span.get_span_context().span_id

          # The battery's own scaffolding spans — the root above and one
          # per phase — must never count as instrumentation the adapter
          # emitted, or every phase would trivially satisfy its own
          # check.
          scaffolding_ids: set[int] = {battery_span_id}

          def _observed_spans() -> list:
              """Spans belonging to THIS run's trace, minus the battery's own.

              In a booted process the exporter also sees spans from
              concurrent requests and background jobs, so filtering by
              trace id is what makes "did the adapter emit a span?" an
              answerable question at all.
              """
              if exporter is None:
                  return []
              return [
                  s
                  for s in exporter.get_finished_spans()
                  if s.context is not None
                  and s.context.trace_id == battery_trace_id
                  and s.context.span_id not in scaffolding_ids
              ]

          def _descends_from(span, ancestor_id: int, by_id: dict) -> bool:
              """Is ``span`` inside ``ancestor_id``'s context?

              Walks the recorded parent chain. ``by_id`` holds only
              FINISHED spans, so an intermediate ancestor still open
              breaks the walk and this answers False — the conservative
              direction, since the alternative is crediting a phase for
              work it may not have done.
              """
              seen: set[int] = set()
              current = span
              while current is not None:
                  parent = current.parent
                  if parent is None:
                      return False
                  if parent.span_id == ancestor_id:
                      return True
                  if parent.span_id in seen:
                      return False  # defensive: a cycle cannot happen, but
                  seen.add(parent.span_id)
                  current = by_id.get(parent.span_id)
              return False

          requested_phases = phases or ["analyze"]
          final_phase = requested_phases[-1]
          prior: dict | None = None
          for phase in requested_phases:
              inp.prior_analysis = prior
              progress_before = len(progress_calls)
              # Each phase runs inside its OWN span, and its spans are
              # attributed by descent from it. Two weaker rules were
              # tried and both certified a phase that emitted nothing:
              #
              #   * a count delta — a span an earlier phase left running
              #     FINISHES during this one and lifts the count;
              #   * `start_time >= phase_started_ns` — an earlier phase's
              #     deferred task OPENS its span during this one, so the
              #     timestamp lands in this phase's window.
              #
              # Both were reproduced before being replaced. Context is
              # the thing that actually answers "whose work is this?":
              # asyncio copies the ambient context when a task is
              # created, so phase one's deferred span is parented to
              # phase one however late it opens. This also matches
              # production more closely — the runner wraps each phase in
              # a span too.
              phase_span_cm = _trace_api.get_tracer(__name__).start_as_current_span(
                  f"adapter_kit:phase:{phase}"
              )
              with phase_span_cm as phase_span:
                  phase_span_id = phase_span.get_span_context().span_id
                  scaffolding_ids.add(phase_span_id)
                  try:
                      out = await agent.run_phase(phase, inp, on_progress)
                      phase_raised = False
                  except Exception as exc:
                      result.failures.append(
                          f"phase {phase!r} raised {_exc_text(exc)}"
                      )
                      phase_raised = True
              if phase_raised:
                  break
              result.phases_run.append(phase)

              # Whether production could have carried this phase's result
              # forward at all. Two independent ways it could not:
              #
              #   1. the status is outside the runner's success vocabulary
              #      (`if not ok ... return`, agent_runner.py:541), or
              #   2. the result cannot be PROCESSED — the runner's very
              #      first act is `dict(result.structured or {})`
              #      (agent_runner.py:493), which raises for a truthy
              #      non-mapping, and a value JSONB cannot hold dies at
              #      the commit below it.
              #
              # Status alone was not enough: a phase returning
              # `status="complete", structured="oops"` reads as fine here
              # and raises there. Emulated with the runner's own
              # expression rather than a rule about it, so the two cannot
              # disagree.
              # Every attribute below belongs to a result object the
              # AGENT defined, so any of them can be a property that
              # raises — and `getattr`'s default only swallows
              # `AttributeError`.
              #
              # Replayed as the runner's STEPS: its reads *and* the
              # operation it performs at each position, in its order.
              # Four shapes were tried and three were detectably wrong —
              # read-each-once, match-the-counts, and reads-in-order —
              # because each fixed a different axis. Testing membership
              # at :495 BEFORE reading `report_html` at :499 is the
              # fourth: a `__hash__` with side effects the report
              # property observes leaves the two in a different state
              # than fetch-everything-then-test.
              is_final_phase = phase == final_phase
              replay = _replay_runner_steps(
                  out,
                  _RUNNER_STEPS_FINAL if is_final_phase else _RUNNER_STEPS_NON_FINAL,
              )

              values: dict[str, Any] = {}
              attr_failures: list[str] = []
              readable = replay.failure is None
              if replay.failure is not None:
                  bad_name, bad_line, bad_what, bad_exc = replay.failure
                  doing = (
                      f"reading it"
                      if bad_what == "read"
                      else f"evaluating {_RUNNER_OPS[bad_what]}"
                  )
                  attr_failures.append(
                      f"phase {phase!r} the runner's step at agent_runner.py:"
                      f"{bad_line} fails on result.{bad_name} — {doing} raises "
                      f"{_exc_text(bad_exc)}. {_RUNNER_READS[bad_name]}, so the "
                      f"run dies there with the phase's work already done"
                  )

              for name, reads in replay.reads.items():
                  # The FIRST value, not the last: that is the one the
                  # runner's outcome hangs on (`ok` at :495, the
                  # persisted `snap.report_html` at :499).
                  values[name] = reads[0] if reads else None
                  if _reads_diverged(reads):
                      # Not fatal — production completes — but the run
                      # page and the trace then describe different
                      # results, which is precisely the kind of quiet
                      # divergence this battery exists to surface.
                      attr_failures.append(
                          f"phase {phase!r} result.{name} changed between the "
                          f"runner's repeated reads "
                          f"({', '.join(_safe_repr(r) for r in reads)}) — the "
                          f"run's outcome follows the FIRST while the trace "
                          f"records a later one, so the two disagree"
                      )

              # Attributes production does NOT read in this phase are
              # still type-checked below, so they need one plain read —
              # and a failure there is reported but NOT fatal, since the
              # run itself survives. Skipped entirely once the replay
              # has already died, because production never got here.
              for name in ("structured", "status", "report_html", "display"):
                  if name in values or replay.failure is not None:
                      values.setdefault(name, None)
                      continue
                  try:
                      values[name] = getattr(out, name, None)
                  except Exception as exc:
                      values[name] = None
                      attr_failures.append(
                          f"phase {phase!r} result.{name} could not be read "
                          f"({_exc_text(exc)}) — AgentProtocol declares it, though "
                          f"the runner does not read it on a "
                          f"{'final' if is_final_phase else 'non-final'} phase, so "
                          f"the run itself survives"
                      )

              structured_out = values["structured"]
              status_value = values["status"]
              report_value = values["report_html"]
              display_value = values["display"]
              result.failures.extend(attr_failures)

              # The membership verdict comes from the replay, which
              # performed it exactly where and as often as production
              # does — once, at agent_runner.py:495. Computing it
              # separately was its own defect twice over: called twice it
              # let a stateful `__hash__` set `phase_fatal` on one call
              # and report nothing on the other, and called after all the
              # reads it ran in a state production never reaches.
              if replay.failure is not None and replay.failure[2] == "membership":
                  # The membership test itself raised — that IS the status
                  # problem, described in its own words.
                  status_problem = _status_message(status_value, replay.failure)
              elif replay.ok is None:
                  # The replay died BEFORE :495, so the test never ran and
                  # `status` was never read. Production died at that same
                  # earlier step. Reporting "returned status None" here
                  # would be a second, false failure stacked on the real
                  # one — a verdict on a value the battery never saw.
                  status_problem = None
              elif replay.ok:
                  status_problem = None
              else:
                  status_problem = _status_message(status_value, None)
              phase_fatal = not readable or status_problem is not None

              # The RESULT of the runner's own `dict(result.structured
              # or {})`, kept rather than recomputed. `dict()` succeeds
              # for more than dicts — a list of pairs, a custom Mapping —
              # and production carries whatever it produced into the next
              # phase; substituting `{}` would give later phases a
              # different `prior_analysis` than the real run, which is the
              # same divergence this battery exists to catch, committed by
              # the battery itself.
              #
              # Performed inside the replay now, at :493, so it happens
              # before the membership test rather than after every read.
              coerced = replay.coerced

              # Per phase, not in aggregate: an agent whose first phase
              # reports once and whose second runs silently for a minute
              # would pass an aggregate check while the run page sits
              # blank for that whole minute.
              if len(progress_calls) == progress_before:
                  result.failures.append(
                      f"phase {phase!r} completed without streaming any progress — "
                      f"the run page would sit blank for its whole duration"
                  )

              # Per phase for the same reason progress is: an aggregate
              # "were there spans?" is satisfied by the FIRST instrumented
              # phase, so an agent that traces its cheap opening phase and
              # runs the expensive one blind reads as conformant while the
              # trace shows a gap exactly where someone would look.
              #
              # Descent from this phase's span is the attribution rule.
              # Known residual, stated rather than left implicit: a span
              # this phase opens and never finishes is invisible here
              # (nothing but finished spans reaches the processor), so a
              # phase whose only instrumentation is still running reads
              # as untraced. That is the conservative direction — it
              # complains about a real oddity rather than certifying
              # silence — and closing it would mean waiting on work the
              # phase itself abandoned.
              observed = _observed_spans()
              by_id = {
                  s.context.span_id: s for s in observed if s.context is not None
              }
              phase_spans = [
                  s for s in observed if _descends_from(s, phase_span_id, by_id)
              ]
              if require_traces and exporter is not None and not phase_spans:
                  result.failures.append(
                      f"phase {phase!r} completed without emitting any spans — "
                      f"the run's trace would have a gap where this phase's work "
                      f"should be"
                  )

              # Deliberately NOT gated on `isinstance`: the runner
              # reaches every field through `getattr` and never checks
              # the class, so a duck-typed result object with the
              # right attributes runs fine in production. Computing
              # `prior`/`persisted` only for the declared classes left
              # a custom result carrying the PREVIOUS phase's
              # prior_analysis into the next phase.
              #
              # Validate the shape BEFORE coercing it. The runner's
              # own `dict(result.structured or {})` raises on a truthy
              # non-mapping like "oops" — so doing it first here would
              # make run_battery raise instead of REPORTING the very
              # conformance failure it is supposed to catch, and the
              # caller would get an exception rather than a
              # BatteryResult listing what is wrong.
              structured_is_dict = _is_a(structured_out, dict)

              # Feed the next phase what the chassis would. The runner
              # stores `snap.analysis = dict(result.structured or {})`
              # for every non-final phase whatever its result TYPE, so
              # a three-phase agent whose middle phase returns an
              # InvestigationResult must still see it as
              # prior_analysis — otherwise the battery exercises
              # different inputs from production.
              # The runner's coerced value, not a re-derivation of it
              # — `dict(structured or {})` is exactly what
              # agent_runner.py:493 stores, whether the agent handed
              # over a dict, a list of pairs or a custom Mapping.
              # `coerced` has ALREADY had `_drifts` removed — the replay
              # does it at :494, where the runner does it, so the pop is
              # not repeated here. Repeating it was harmless in effect
              # and wrong in position: production strips the key BEFORE
              # the status membership test, so a key whose `__eq__`
              # mutates shared state was observed in the wrong order by
              # everything downstream — and a key whose `__eq__` raises
              # escaped `run_battery` entirely from this line.
              prior = dict(coerced) if coerced is not None else {}

              # What the chassis actually PERSISTS, which is not what
              # the agent returned: the runner strips `_drifts`
              # (agent_runner.py:494) before `snap.structured_data =
              # structured`. So a final phase returning only
              # `{"_drifts": [...]}` is truthy here and lands as `{}`
              # there — a complete run with nothing to render. Every
              # check below reads this rather than the raw value; the
              # stripping already existed for `prior` and simply was
              # not carried across to the output checks.
              persisted = prior if coerced is not None else structured_out

              # The runner treats anything outside its success
              # vocabulary as a failed run, so a phase returning
              # e.g. "failed" must not pass here just because it is
              # not the literal string "error" — and an unhashable
              # status must be reported rather than raised, which is
              # why this goes through the shared helper.
              if status_problem:
                  # `error` is optional diagnostic detail the agent
                  # chooses, and the previous version touched it three
                  # unguarded ways at once: `getattr` (a raising property
                  # propagates), truthiness (`__bool__` is the agent's),
                  # and bare interpolation (`str()` raises on
                  # `error=10**5000`). Production never reads this field
                  # at all — it records the failed status and exits — so
                  # the battery was the only thing dying on it.
                  detail = _attr_of(out, "error")
                  result.failures.append(
                      f"phase {phase!r} {status_problem}"
                      + ("" if _is_empty(detail) else f": {_safe_str(detail)}")
                  )

              # Only when the replay COMPLETED. A failed step means
              # production died there, so it never reached these
              # fields either — and the values below were never read,
              # so type-checking them invents failures (a `None`
              # `display` reported as "not a dict") on top of the real
              # one that is already recorded.
              if readable:
                  if _is_a(out, AnalysisResult):
                      if not _is_a(structured_out, dict):
                          result.failures.append(
                              f"phase {phase!r} structured output is not a dict"
                          )
                      if not _is_a(display_value, dict):
                          result.failures.append(f"phase {phase!r} display output is not a dict")
                      # `persisted`, not `out.structured`: the chassis strips
                      # `_drifts` before storing, so this is the answer a user
                      # would actually see.
                      result.outputs[phase] = persisted
                  elif _is_a(out, InvestigationResult):
                      if structured_out is not None and not _is_a(structured_out, dict):
                          # Same rule as AnalysisResult, and for a concrete
                          # reason: the runner does dict(result.structured),
                          # which raises for a str/list and fails the run. The
                          # battery must not certify output the chassis cannot
                          # persist.
                          result.failures.append(
                              f"phase {phase!r} structured output is "
                              f"{_safe_type_name(structured_out)}, not a dict — the "
                              f"chassis runner cannot persist it"
                          )
                      # `persisted`, not `out.structured`: the chassis strips
                      # `_drifts` before storing, so this is the answer a user
                      # would actually see.
                      result.outputs[phase] = persisted
                  else:
                      # Reported as a contract violation, but NOT fatal by
                      # itself: the runner duck-types through `getattr`, so a
                      # custom object with a good status and coercible
                      # `structured` is persisted and advanced exactly like a
                      # declared result. `phase_fatal` is already set above
                      # from the same expressions the runner uses, so
                      # whatever production would do, the battery does.
                      result.failures.append(
                          f"phase {phase!r} returned {_safe_type_name(out)}, expected "
                          f"AnalysisResult or InvestigationResult"
                      )
                      result.outputs[phase] = persisted

                  # `report_html` is duck-typed by the runner exactly like
                  # `status` and `structured`, so its check cannot live in a
                  # result-type branch either. Under `if is_final:` the
                  # runner does `len(getattr(result, "report_html", None) or
                  # "")` (agent_runner.py:517-519) for EVERY result type, so
                  # an AnalysisResult subclass carrying `report_html=1`
                  # reaches that `len()` and errors the run with the work
                  # already done. Nothing else catches it: the renderability
                  # check below only tests truthiness, and `1` is truthy.
                  #
                  # This was the last of the class-gated checks — the data
                  # path came off `isinstance` last round and this one was
                  # left behind, which is the same miss twice.
                  if report_value is not None and not _is_a(report_value, str):
                      result.failures.append(
                          f"phase {phase!r} report_html is "
                          f"{_safe_type_name(report_value)}, not a string — the "
                          f"chassis persists it as text and measures its length"
                      )
                      # Fatal only where the field is actually consumed. The
                      # runner reads `report_html` under `if is_final:` alone
                      # (agent_runner.py:499 and the `len()` below it); a
                      # non-final phase stores `snap.analysis` and advances
                      # without ever touching it. Aborting here would make
                      # the battery stricter than production and skip later
                      # phases the real run performs.
                      if phase == final_phase:
                          phase_fatal = True
                  elif _is_a(report_value, str):
                      # A string the Text column cannot hold. Same two
                      # characters as JSONB — measured, not assumed — so the
                      # report field needs this check as much as `structured`
                      # does; it was found for `structured` and would have
                      # been missed here on the usual "fix only what was
                      # reported" reflex.
                      text_problem = _pg_text_problem(report_value)
                      if text_problem:
                          result.failures.append(
                              f"phase {phase!r} report_html {text_problem} — the "
                              f"chassis stores it in a Text column, so the commit "
                              f"fails after the phase has done its work"
                          )
                          if phase == final_phase:
                              phase_fatal = True

                  # The manifest, not the result, decides which field the run
                  # page reads — so the final phase must fill the one the
                  # declared mode renders from. ResultsView.tsx takes the
                  # structuredMode branch and never fetches the HTML, so
                  # `structured={}` plus a full report_html is a COMPLETE run
                  # showing "No structured result available.": output that
                  # exists and is ignored. Checked here rather than inside a
                  # result-type branch, because a final phase may return
                  # either type and the rule is the same for both.
                  if phase == final_phase:
                      report_out = report_value
                      structured_final = result.outputs.get(phase)
                      if output_mode is not None:
                          rendered = (
                              structured_final
                              if output_mode == "structured"
                              else report_out
                          )
                          if _is_empty(rendered):
                              field = (
                                  "structured output"
                                  if output_mode == "structured"
                                  else "report_html"
                              )
                              result.failures.append(
                                  f"phase {phase!r} produced no {field} ({_safe_repr(rendered)}), "
                                  f"but the agent declares output.mode={output_mode} — "
                                  f"that is the field the run page renders from, "
                                  f"whatever the other one contains"
                              )
                      elif not structured_final and _is_empty(report_out):
                          # No declared mode: the weaker check, that SOMETHING
                          # renderable came back. `not structured` rather than
                          # `is None`, because an empty dict is just as empty
                          # to a reader — and read from `out` generically,
                          # because an `AnalysisResult` can be a final result
                          # too and has no report field to fall back on. This
                          # check lived inside the InvestigationResult branch
                          # until review pointed out that the mode check had
                          # been lifted out for exactly this reason while its
                          # sibling was left behind.
                          result.failures.append(
                              f"phase {phase!r} produced no structured output "
                              f"({structured_final!r}) and no report_html — the run "
                              f"would be marked complete with nothing to render"
                          )

                  # Top-level shape is not enough. The runner writes this
                  # value into a JSONB column, so a set, a UUID or a NaN
                  # nested anywhere inside an otherwise ordinary dict fails
                  # the run at COMMIT — with the work already done. Framework
                  # -agnostic on purpose: the LangGraph adapter coerces its
                  # own output, but the battery must catch this for adapters
                  # that don't, which is every one not yet written.
                  if phase in result.outputs:
                      try:
                          # THE COMMITTED TEXT IS THE VERDICT, whenever
                          # there is one. Production serialized once at
                          # :509 and the database received exactly that
                          # string; anything a later look at the live
                          # object says is about a value that may never
                          # have been stored.
                          #
                          # Checked ALWAYS, not only to confirm a problem
                          # the live walk already found. A container that
                          # emits a NaN during the commit and safe data
                          # afterwards produces no finding from the live
                          # walk at all, and PostgreSQL still rejects the
                          # payload the commit actually sent.
                          #
                          # `json.loads` of it is inert — no agent code,
                          # no further traversal — and it preserves NaN,
                          # NUL and unpaired surrogates, which are exactly
                          # what `json.dumps` accepts and PostgreSQL
                          # refuses.
                          #
                          # ONE parse, answering both questions below.
                          # The encodability verdict reads the parsed
                          # value; the collision verdict reads the PAIRS
                          # the parser saw, because parsing is itself
                          # what destroys the evidence of a collision.
                          committed = None
                          committed_lossy = None
                          committed_dropped = None
                          have_commit = False
                          if replay.persisted and replay.serialized is not None:
                              parsed = _committed_json(replay.serialized)
                              if parsed is not None:
                                  committed, committed_lossy, committed_dropped = parsed
                                  have_commit = True

                          # `have_commit`, not `committed is not None`:
                          # a text of `null` parses to None, and reading
                          # "there was no commit" out of that is the same
                          # inference-from-absence the persist flag was
                          # split out to stop.
                          if have_commit:
                              problem = _json_problem(
                                  committed,
                                  # The replay sorts at :517 where the
                                  # runner sorts; the walk must not do it
                                  # again on the agent's own keys.
                                  check_top_level_order=False,
                              )
                              if problem is None:
                                  # A value the parse threw away when it
                                  # collapsed a duplicate name. It is not
                                  # in `committed` to be found — and the
                                  # database reads the text, not the
                                  # parse, so it kills the commit all the
                                  # same.
                                  problem = committed_dropped
                          else:
                              # No commit to judge — the replay never
                              # reached :509, or its output was not
                              # reloadable. Fall back to the live object,
                              # which is all there is.
                              problem = _json_problem(
                                  result.outputs[phase],
                                  check_top_level_order=False,
                              )
                              if isinstance(problem, _Uninspectable) and replay.persisted:
                                  problem = None
                          if problem is None and replay.persist_error is not None:
                              # The commit raised during the replay and the
                              # walk cannot see why. That gap is the whole
                              # point of recording it: a container that
                              # raises only on its FIRST serialization lets
                              # this second look succeed, and without this
                              # the phase would be certified while
                              # production stopped at the failed commit.
                              #
                              # Asking twice and believing the second answer
                              # is the mistake this battery exists to catch
                              # in adapters. It is not allowed here either.
                              problem = (
                                  f"the commit at agent_runner.py:509 raised "
                                  f"{_exc_text(replay.persist_error)}, but serializing "
                                  f"the same output again succeeded — the value "
                                  f"answers differently each time it is read, so the "
                                  f"run dies at COMMIT while any later inspection "
                                  f"looks clean"
                              )
                          if problem:
                              result.failures.append(
                                  f"phase {phase!r} produced output the chassis cannot "
                                  f"persist — the runner assigns it to a JSONB column, so "
                                  f"this fails at COMMIT after the phase has done its "
                                  f"work: {problem}"
                              )
                              phase_fatal = True

                          # Reported, never fatal: JSON resolves a key collision
                          # by overwriting rather than raising, so production
                          # completes the run and merely loses a value. Aborting
                          # here would be the battery being stricter than the
                          # chassis all over again.
                          #
                          # From the COMMITTED TEXT for the same reason the
                          # check above is, and this was the half left
                          # behind when that one moved: a `dict` subclass
                          # whose first `items()` yields colliding pairs
                          # and whose later calls yield safe ones commits
                          # `{"1": "first", "1": "second"}` — JSONB keeps
                          # the last and the value is gone — while this
                          # walk, arriving afterwards, saw only the safe
                          # answer. Fixing the sibling a review named and
                          # leaving its twin reading the live object is
                          # the "guard the SET, not the branch" lesson
                          # arriving a round late.
                          #
                          # It cannot come from `committed`: parsing is
                          # what DESTROYS the collision, so the duplicate
                          # is found in the parser's pairs instead. See
                          # `_committed_json`.
                          if have_commit:
                              lossy = committed_lossy
                          else:
                              lossy = _json_lossy(result.outputs[phase])
                          if lossy:
                              result.failures.append(
                                  f"phase {phase!r} produced output whose keys collide "
                                  f"once encoded — the run completes and the chassis "
                                  f"stores a value the agent did not produce: {lossy}"
                              )
                      except Exception as exc:
                          if replay.persisted:
                              # Same reasoning as the `_Uninspectable` case
                              # above: production committed this value and
                              # never walks it again, so a crash in the
                              # battery's own second traversal is ours, not
                              # the agent's.
                              exc = None
                          # One backstop over the WHOLE inspection, not per walker.
                          # `_json_problem` carries its own boundary and reports a
                          # precise path, so it does not reach here — but it was never
                          # the only walk over this value. `_json_lossy` iterates the
                          # same agent-supplied objects with the same operations, and
                          # guarding only the walk the review named would have left
                          # its sibling escaping on the identical input.
                          #
                          # There is no enclosing `try` between here and
                          # `run_battery`'s signature — measured, not assumed — so an
                          # escape hands the adapter author a traceback out of the
                          # battery instead of a failed `BatteryResult` naming their
                          # own defect.
                          if exc is not None:
                              result.failures.append(
                                  f"phase {phase!r} produced output the battery could not "
                                  f"inspect — {_exc_text(exc)}. The chassis runs these same "
                                  f"operations to persist the result, so this raises there "
                                  f"too, after the phase has done its work"
                              )
                              phase_fatal = True

                  # Production would never reach the next phase, so neither
                  # does the battery. Continuing would run model calls and
                  # capability side effects — with their costs — down a path
                  # the real run cannot take, and then report observations
                  # from it as if they meant something.
              if phase_fatal:
                  break


        # Read inside the capture scope — the processor is shut down on
        # the way out of `_span_capture`, and a shut-down exporter has
        # released what it held. The battery's own root span is excluded
        # by `_observed_spans`, otherwise it would satisfy the check by
        # itself.
        spans = None if exporter is None else _observed_spans()

    # --- progress ----------------------------------------------------
    if not progress_calls:
        result.failures.append(
            "no progress was streamed — the run page would sit blank for the "
            "whole run, which is the failure mode this check exists for"
        )

    # --- traces ------------------------------------------------------
    if spans is None:
        # No SDK provider and no way to install one without leaking it.
        # Say so rather than passing quietly: an unobserved check that
        # reports success is the failure mode this batch keeps meeting.
        if require_traces:
            result.failures.append(
                "spans could not be observed: this process has no SDK tracer "
                "provider. The kit deliberately does not install one — doing "
                "so would claim the process-global provider and could leave a "
                "later application's real exporter ignored. Initialize "
                "tracing before running the battery, or pass "
                "require_traces=False and prove traces elsewhere"
            )
    else:
        result.span_names = [s.name for s in spans]
        result.event_names = [
            e.name for s in spans for e in (getattr(s, "events", None) or ())
        ]
        if require_traces and not spans:
            result.failures.append(
                "no spans were emitted during the run — the run's trace link "
                "would lead nowhere"
            )

    return result
