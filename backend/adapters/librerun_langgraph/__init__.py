"""``librerun-langgraph`` — run a compiled LangGraph graph as a LibreRun agent.

The first of the adapter family (blueprint B16, decision L11). It exists
so that "bring your agent, whatever the framework" is true for LangGraph
without the chassis knowing what a graph is: the chassis sees an
``AgentProtocol``, LangGraph sees a normal compiled graph, and this
module is the whole of the seam between them.

Minimal use — a single-phase agent::

    from librerun_langgraph import LangGraphAgent

    agent = LangGraphAgent(
        agent_id="triage-v1",
        display_name="Triage",
        description="Classifies an incident and drafts next steps",
        input_schema={...},              # JSON Schema for intake
        graph=my_compiled_graph,         # StateGraph(...).compile()
    )

Multi-phase, mapped to the phases the manifest declares::

    agent = LangGraphAgent(
        ...,
        graphs={"analyze": triage_graph, "investigate": deep_graph},
    )

Three things the adapter is responsible for, because they are what the
platform promises and a bare graph does not provide:

* **Progress.** The graph is driven with ``astream(...,
  stream_mode=["updates", "values"])`` — the per-node updates so every
  node completion becomes a ``StepProgress``, and LangGraph's own
  reduced values so the answer survives reducer-backed channels intact.
  A graph that runs for a minute reports each node as it lands instead
  of going quiet. The same completions are recorded as timestamped
  events on the phase span — events rather than spans because the
  stream reports a node only once it has finished, so any span opened
  then would carry a fabricated duration.
* **Output shape.** The final state is mapped onto the chassis' result
  types and coerced JSON-safe, so a graph's free-form state cannot reach
  the run page as something the UI can't render, nor fail the run at the
  database.
* **Capabilities.** The per-run capability façade (B13) is passed in the
  run config, where a node reaches it with ``capabilities_of(config)``,
  so nodes use ``kb`` / ``run_store`` / ``audit`` under the manifest's
  grant — in-process here, and over MCP for container agents, with no
  per-framework glue. Config rather than state because a checkpointer
  persists state, and this façade is both unserializable and scoped to a
  single run.

The chassis has no LangGraph dependency and must not gain one: this
package is imported only by agents that choose it.
"""
from __future__ import annotations

import math
from collections.abc import Mapping as _MappingABC, Sequence as _SequenceABC
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping
from uuid import UUID

from opentelemetry import trace

from app.agents.protocol import (
    AgentInput,
    AgentProtocol,
    AnalysisResult,
    InvestigationResult,
    OnProgress,
    StepProgress,
)

_tracer = trace.get_tracer(__name__)

# Keys the adapter writes into the graph's initial state. Documented
# because a graph author has to read them, and named so they are
# unlikely to collide with a graph's own state. All five are plain JSON
# data — which is the rule, not a coincidence: state is what a
# checkpointer persists.
STATE_USER_INPUTS = "user_inputs"
STATE_PRIOR = "prior_analysis"
STATE_EDITS = "user_edits"
STATE_RUN_ID = "run_id"
# The pre-S1 spelling of the same channel (blueprint S1, L18). LangGraph
# drops input keys a graph's state schema does not declare, so a graph
# written against the earlier contract — `case_id: str` in its state —
# would receive nothing under the new name alone. Both are injected for
# one release and both are adapter-owned; the legacy key goes at v1.1.
STATE_RUN_ID_LEGACY = "case_id"

# Capabilities travel in the RUN CONFIG, not in state, and the
# distinction is load-bearing in two ways.
#
# Mechanically: a checkpointer serializes state, and the B13 façade
# holds live chassis service handles. LangGraph's serializer refuses it
# outright — `TypeError: Type is not msgpack serializable: Capabilities`
# — so a graph compiled with a checkpointer would die on its first
# checkpoint, before any node's work could be saved.
#
# And correctly: the façade is scoped to ONE run, carrying that run's
# run id, tenant id and capability grants. Persisting it would mean a
# checkpoint resumed later could act under a scope captured at write
# time. Runtime-only values belong in config; a checkpoint should hold
# the graph's data and nothing else.
CONFIG_CAPABILITIES = "capabilities"

# Where the adapter looks for the agent's answer in the final state,
# in order. A graph that writes none of these still runs — the whole
# final state becomes the structured output, which is the least
# surprising fallback.
OUTPUT_KEYS = ("structured", "result", "output")
REPORT_KEYS = ("report_html", "html")

# Deep enough for any realistic agent output, and a hard stop for a state
# that references itself — which a graph carrying framework objects can
# easily do.
_MAX_JSON_DEPTH = 12


def _safe_type_name(value: Any) -> str:
    """The class name of a value, when even that may not render.

    The last assumption every fallback below makes. A metaclass can
    define ``__name__`` as a property that raises or returns a
    non-string, so the ultimate fallback names nothing rather than
    trusting the name.
    """
    try:
        name = type(value).__name__
    except Exception:
        return "?"
    # `type(name) is str`, not `isinstance`: a metaclass may hand back a
    # `str` SUBCLASS, and every caller interpolates this into an
    # f-string — which runs that subclass's `__format__`.
    return name if type(name) is str else "?"


def _as_exact_str(value: Any) -> str | None:
    """The exact ``str`` behind a value, or ``None`` if there isn't one.

    ``isinstance(x, str)`` is this module's licence to call string
    methods, and it covers two different things: a genuine ``str``
    **subclass**, which owns ``encode``/``replace``/``__contains__``/
    ``__format__`` but really does carry string data, and an object
    whose ``__class__`` property merely *says* ``str``, which is not a
    string at all.

    Measured — only two primitives yield an exact ``str`` without
    running a line of subclass code::

        str.__str__(v)                    -> str   OK
        str.__getitem__(v, slice(None))   -> str   OK
        str(v), v[:], "" + v, "%s" % v, f"{v}"     all run the override

    Returning ``None`` rather than a substitute keeps this usable as the
    single primitive under all three renderers below, which is what
    makes their dependencies acyclic.
    """
    if type(value) is str:
        return value
    try:
        return str.__str__(value)
    except Exception:
        return None


def _safe_repr(value: Any) -> str:
    """``repr`` that cannot raise and cannot return a ``str`` subclass.

    This is where ``_json_safe_value`` sends anything it cannot convert
    structurally, so it runs on the most hostile values the graph
    carries, and its result goes straight into ``_pg_safe_text`` and
    into f-strings.

    ``repr()`` guarantees a ``str`` INSTANCE — which includes a
    subclass, measured — so normalizing only the failure path left the
    SUCCESS path handing the hazard back out.
    """
    try:
        rendered = repr(value)
    except Exception:
        rendered = None
    if rendered is not None:
        exact = _as_exact_str(rendered)
        if exact is not None:
            return exact
    if isinstance(value, int):
        try:
            return f"<int too large to serialize: {value.bit_length()} bits, 0x{value:x}>"
        except Exception:
            pass
    return f"<unrepresentable {_safe_type_name(value)}>"


def _safe_text(value: Any) -> str:
    """``str`` of any value, which must never raise — for ANY reason.

    Converting a graph's state runs that state's own code, in three
    unrelated ways:

      * CPython caps int-to-string conversion at 4300 digits, so
        ``str()`` and ``repr()`` raise on something like ``10**5000``;
        hex formatting and ``bit_length()`` are exempt, which is what
        makes a description possible at all.
      * a node's own ``__str__`` may raise anything, or return a
        non-string, which makes the ``str`` builtin raise ``TypeError``.
      * ``__str__`` may return a ``str`` **subclass**, which the builtin
        accepts and passes on to ``_pg_safe_text``.

    Depends on ``_safe_repr`` and never the reverse. The previous shape
    had the two calling each other: for an object that lies about
    ``__class__`` *and* refuses ``repr``, one render made **249**
    recursive calls and terminated only because ``RecursionError`` is an
    ``Exception`` and one of the guards swallowed it. Using the
    recursion limit as control flow is not a base case.
    """
    if isinstance(value, str):
        exact = _as_exact_str(value)
        if exact is not None:
            return exact
    try:
        rendered = str(value)
    except Exception:
        rendered = None
    if rendered is not None:
        exact = _as_exact_str(rendered)
        if exact is not None:
            return exact
    if isinstance(value, int):
        try:
            return f"<int too large to serialize: {value.bit_length()} bits, 0x{value:x}>"
        except Exception:
            pass
    return _safe_repr(value)


def _exact_str(text: Any) -> str:
    """``_as_exact_str`` for the places that only need to RENDER.

    A value with no string behind it degrades to its ``repr`` here,
    which ``_safe_repr`` has already normalized.
    """
    exact = _as_exact_str(text)
    return exact if exact is not None else _safe_repr(text)


def _is_a(value, types) -> bool:
    """``isinstance`` that cannot raise — ``__class__`` is graph-owned code.

    ``isinstance`` consults ``__class__``, and a graph can write a state
    value whose ``__class__`` is a property that raises. The conversion
    helpers below already sit behind a boundary that turns such a raise
    into a rendered fallback, but the checks in ``_to_result`` and the
    streaming loop run BEFORE that boundary: a raise there escapes the
    adapter after the graph has finished all its work, and the runner
    marks the phase errored instead of degrading the value to something
    persistable — which is the whole job of this class.

    Unaskable counts as False, so the value takes the same path as any
    other non-matching type: wrapped, or dropped to ``None``, and then
    coerced by ``_json_safe``.
    """
    try:
        return isinstance(value, types)
    except Exception:
        return False


def _pg_safe_text(text: str) -> str:
    """Make a string the chassis can actually persist.

    Two characters encode cleanly through ``json.dumps`` and are then
    refused by PostgreSQL — measured against a live PG16, not inferred:
    an embedded NUL (``\\u0000 cannot be converted to text``) and an
    unpaired surrogate (not valid UTF-8). Both are rejected by JSONB and
    by ``Text`` columns alike, so both would fail the commit after the
    graph had finished.

    Repaired rather than rejected, on this module's existing rule: the
    graph did nothing wrong, and a dead run is a worse answer than a
    marked-up one. Each offending character becomes its literal escape
    text, so nothing is silently dropped and the reason stays visible in
    the output — the same choice made for colliding mapping keys below.

    Normalized on entry because every string operation below —
    ``in``, ``replace``, ``encode`` — is overridable by a ``str``
    subclass, and this function is where all of them are invoked.
    """
    text = _exact_str(text)
    if "\x00" in text:
        text = text.replace("\x00", "\\u0000")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def _json_safe(value: Any, _depth: int = 0) -> Any:
    """Coerce a graph's state into something the chassis can persist.

    A thin wrapper on purpose. ``_json_safe_value`` below returns strings
    from **eight** different branches — the string fast path, decoded
    bytes, ``str()`` of a UUID or Decimal, ``isoformat()``, three
    ``repr`` fallbacks and the depth cutoff — and sanitizing them one
    call site at a time is how this function accrued three separate
    review findings for the same defect. Repairing at the single exit
    instead means a branch added later cannot reintroduce it: whatever
    path produced the string, it is storable by the time it leaves here.

    Mapping keys are the one thing this cannot cover, since the mapping
    branch builds them itself; they are repaired there.
    """
    out = _json_safe_value(value, _depth)
    return _pg_safe_text(out) if _is_a(out, str) else out


# Sentinel: "this is not a scalar", distinct from a converted `None`.
_UNCONVERTED = object()


def _scalar_json_value(value: Any) -> Any:
    """The JSON form of a scalar, or ``_UNCONVERTED`` if it is not one.

    Every branch is entered on ``isinstance``, and **``isinstance`` only
    consults ``__class__``** — which any object can define. So each of
    these branches is reachable by something that is not the type it
    claims, and the type-specific operation then raises: ``isfinite``
    wants a real number, ``bytes()`` wants real bytes, ``int.__repr__``
    is a descriptor bound to ``int``.

    This function is therefore allowed to raise; the caller degrades to
    ``repr``. Collecting the scalar branches here is the point — the
    alternative is patching one branch per review round, which is
    exactly what happened for ``bool``, then ``float``, then ``bytes``.
    """
    if isinstance(value, int):
        # The integer twin of the NaN case: a valid Python int that
        # `json.dumps` refuses. Since 3.11 CPython caps int-to-string at
        # 4300 digits, so `10**5000` raises during the runner's JSONB
        # write — after the graph has finished. `bit_length()` and hex
        # formatting are exempt, which is what makes a description
        # possible. `int.__repr__` is what json itself calls.
        try:
            int.__repr__(value)
        except ValueError:
            return _safe_text(value)
        return value
    if isinstance(value, float):
        # NaN/Infinity are valid Python floats and invalid JSON; the
        # driver rejects them on the way into JSONB.
        return value if math.isfinite(value) else _safe_text(value)
    if isinstance(value, (UUID, Decimal)):
        return _safe_text(value)
    if isinstance(value, (datetime, date, time)):
        stamp = value.isoformat()
        return _exact_str(stamp) if isinstance(stamp, str) else _safe_text(stamp)
    if isinstance(value, (bytes, bytearray)):
        # Converted ONCE. The old shape called `bytes(value)` again in
        # its `UnicodeDecodeError` handler, so a value whose conversion
        # was the problem raised a second time from the recovery path.
        raw = bytes(value)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return _safe_repr(raw)
    return _UNCONVERTED


def _json_safe_value(value: Any, _depth: int = 0) -> Any:
    """Coerce a graph's state into something the chassis can persist.

    The chassis writes an agent's structured output straight into JSONB
    columns, so a value ``json`` cannot encode does not fail politely: it
    fails at COMMIT, after the work is done, leaving the run in error
    with nothing to show for it.

    That is not an exotic shape for LangGraph — it is the most common
    one. A graph on ``MessagesState`` (or any ``add_messages`` channel)
    that writes no explicit output key falls through to the whole-state
    fallback below, and that state is full of ``HumanMessage`` /
    ``AIMessage`` objects.

    So convert rather than reject: refusing would fail the most ordinary
    LangGraph agent there is, and the graph did nothing wrong. JSON-native
    values pass through unchanged, pydantic models (which LangChain
    messages are) become their own fields, and anything else degrades to
    ``repr`` — a run page showing ``"<Foo object at 0x…>"`` is a poor
    answer, but it is an answer, and the alternative is a dead run.
    """
    if _depth > _MAX_JSON_DEPTH:
        return _safe_repr(value)
    # Strings are repaired by the wrapper above, not here — every branch
    # in this function that produces one is covered by that single exit.
    # "JSON-safe" is not the same promise as "storable": a NUL or an
    # unpaired surrogate encodes through `json.dumps` without complaint
    # and PostgreSQL then refuses it, killing the run at COMMIT with the
    # graph's work already done. A node handling binary-ish text (a
    # decoded payload, a truncated log) produces these without doing
    # anything wrong.
    # `type(value) is bool`, NOT `isinstance`: `bool` cannot be
    # subclassed, so anything that merely *claims* to be one is an
    # object lying through `__class__` — and returning it unchanged
    # hands the runner a value `json.dumps` refuses, after the graph has
    # finished. The string path already made this distinction (a real
    # subclass is normalized, a liar degrades to `repr`); the JSON-native
    # fast path had not.
    if value is None or type(value) is bool:
        return value
    if isinstance(value, str):
        # Returned as-is here and normalized at the single exit in
        # `_json_safe`, which is also where a `__class__` liar is caught.
        return value

    try:
        scalar = _scalar_json_value(value)
    except Exception:
        # The value claimed a type through `isinstance` and then refused
        # that type's own operations — `math.isfinite`, `bytes()`,
        # `int.__repr__`, `isoformat` — so it is not that type.
        #
        # Guarded HERE, once, rather than branch by branch as review
        # finds them: `isinstance` consults `__class__`, which any
        # object can define, so every one of those branches is
        # enterable by something that is not the type it claims. Three
        # separate rounds patched three separate branches; this is the
        # narrowest point all of them pass through, and `repr` is
        # already this module's standing answer for anything it cannot
        # convert structurally.
        return _safe_repr(value)
    if scalar is not _UNCONVERTED:
        return scalar

    try:
        return _structural_json_value(value, _depth)
    except Exception:
        # Same boundary as the scalar set, for the same reason: the
        # structural branches are entered on `isinstance` too, and
        # `isinstance(x, dict)` or the `Mapping` ABC check is satisfied
        # by anything whose `__class__` says so — after which
        # `.items()`, `.value`, iteration or `asdict()` raises.
        #
        # Guarding the scalars and leaving these was half a fix: the
        # very next lied-about type simply landed one branch further
        # down. The whole conversion now has one boundary, and `repr`
        # is this module's standing answer on the other side of it.
        return _safe_repr(value)


def _structural_json_value(value: Any, _depth: int) -> Any:
    """Convert a container, model or dataclass — or degrade trying.

    Allowed to raise; the caller degrades to ``repr``. Every branch is
    entered on ``isinstance``, so every one of them is reachable by an
    object that is not what it claims.
    """
    if isinstance(value, Enum):
        # Structural, not scalar: the member's VALUE goes back through
        # the full conversion, so an Enum wrapping a hostile object is
        # handled by the same machinery as that object anywhere else.
        return _json_safe(value.value, _depth + 1)
    if isinstance(value, _MappingABC):
        # JSON object keys are strings; a graph keyed by tuples or ints
        # would otherwise encode fine in Python and not at all in JSON.
        #
        # Stringifying can COLLIDE, though — `{1: "numeric", "1": "text"}`
        # has two distinct keys with one string form — and a dict
        # comprehension would silently drop one value. The result would
        # be perfectly JSON-safe and quietly missing an answer, which is
        # worse than the error it replaced. Colliding keys are suffixed
        # with their original type instead, so nothing is lost and the
        # reason is visible in the output.
        coerced: dict[str, Any] = {}
        for key, item in value.items():
            # Sanitized too: a key is as unstorable as a value, and
            # `str(key)` on an already-bad string returns it unchanged.
            name = _pg_safe_text(_safe_text(key))
            if name in coerced:
                # Built from the SANITIZED name, not the raw key: the
                # disambiguating branch was still interpolating
                # `str(key)`, so a colliding unstorable key came back
                # unrepaired — the same defect one line below its own fix.
                base = name
                name = f"{base}#{_safe_type_name(key)}"
                suffix = 2
                while name in coerced:
                    name = f"{base}#{_safe_type_name(key)}{suffix}"
                    suffix += 1
            coerced[name] = _json_safe(item, _depth + 1)
        return coerced
    if isinstance(value, (list, tuple, set, frozenset)) or (
        isinstance(value, _SequenceABC) and not isinstance(value, (str, bytes))
    ):
        return [_json_safe(v, _depth + 1) for v in value]
    # Pydantic models — LangChain messages among them — carry their real
    # fields, which are worth far more on the run page than a repr.
    for method in ("model_dump", "dict"):
        dump = getattr(value, method, None)
        if not callable(dump):
            continue
        try:
            dumped = dump()
        except Exception:
            continue
        if isinstance(dumped, _MappingABC):
            return _json_safe(dumped, _depth + 1)
    if is_dataclass(value) and not isinstance(value, type):
        try:
            return _json_safe(asdict(value), _depth + 1)
        except Exception:
            pass
    return _safe_repr(value)


class LangGraphAgent(AgentProtocol):
    """Adapt one or more compiled LangGraph graphs to ``AgentProtocol``."""

    def __init__(
        self,
        *,
        agent_id: str,
        display_name: str,
        description: str,
        input_schema: dict,
        graph: Any = None,
        graphs: Mapping[str, Any] | None = None,
        final_phase: str | None = None,
        recursion_limit: int | None = None,
    ) -> None:
        if graph is None and not graphs:
            raise ValueError("LangGraphAgent needs either graph= or graphs=")
        if graph is not None and graphs:
            raise ValueError("pass graph= or graphs=, not both")

        self.agent_id = agent_id
        self.display_name = display_name
        self.description = description
        self._input_schema = input_schema
        # Single-phase agents get the chassis' default phase name, which
        # keeps the simplest manifest (`phases: [{name: analyze}]`)
        # working with no extra configuration.
        self._graphs: dict[str, Any] = dict(graphs) if graphs else {"analyze": graph}
        # The last declared phase produces the run's final answer. Made
        # explicit rather than inferred at call time so a single-phase
        # agent's only phase is correctly treated as final.
        self._final_phase = final_phase or list(self._graphs)[-1]
        self._recursion_limit = recursion_limit

    # -- chassis surface ------------------------------------------------

    def input_schema(self) -> dict:
        return self._input_schema

    @property
    def phase_names(self) -> list[str]:
        """Phase names this agent serves, in order — mirror these in the
        manifest's ``phases:`` list."""
        return list(self._graphs)

    async def run_phase(
        self, phase_name: str, inp: AgentInput, on_progress: OnProgress
    ) -> AnalysisResult | InvestigationResult:
        """Drive the phase's graph, streaming node completions as progress.

        Overrides the protocol default (which dispatches to a method of
        the same name) because an adapter's phases are data, not
        methods — the graphs are supplied at construction time.
        """
        graph = self._graphs.get(phase_name)
        if graph is None:
            raise NotImplementedError(
                f"{self.agent_id} has no graph for phase {phase_name!r}; "
                f"it serves {self.phase_names}"
            )

        state = {
            STATE_USER_INPUTS: inp.user_inputs,
            STATE_PRIOR: inp.prior_analysis,
            STATE_EDITS: inp.user_edits,
            STATE_RUN_ID: str(inp.run_id),
            STATE_RUN_ID_LEGACY: str(inp.run_id),
        }
        config: dict[str, Any] = {
            # A graph compiled with a checkpointer REQUIRES a thread id
            # and raises before running a single node without one, so the
            # adapter always supplies it rather than leaving every author
            # who adds persistence to discover the traceback.
            #
            # Scoped per phase, not per run: each phase is a DIFFERENT
            # graph with different node names, so pointing both at one
            # thread would have the second try to resume the first's
            # checkpoint. Re-running a phase reuses its thread, which is
            # what a checkpointer is for — the graph resumes with its
            # accumulated state and sees the new `user_edits` as input.
            # Graphs compiled without a checkpointer ignore the key.
            "configurable": {
                "thread_id": f"{inp.run_id}:{phase_name}",
                CONFIG_CAPABILITIES: inp.capabilities,
            },
        }
        if self._recursion_limit is not None:
            config["recursion_limit"] = self._recursion_limit

        merged_updates: dict[str, Any] = dict(state)
        final_values: dict[str, Any] | None = None
        # How many times each node has reported in THIS phase. Cyclic
        # graphs are the norm rather than the exception — the ReAct loop
        # runs `agent` and `tools` repeatedly — and the chassis writes
        # progress with HSET keyed on step_id, so every iteration after
        # the first would overwrite its predecessor and the run page
        # would show one completion for a node that ran ten times.
        node_runs: dict[str, int] = {}
        with _tracer.start_as_current_span(
            f"langgraph:{phase_name}",
            attributes={"agent.id": self.agent_id, "phase": phase_name},
        ) as phase_span:
            # Both stream modes at once, because they answer different
            # questions and only LangGraph can answer the second:
            #
            #   "updates" -> {node: delta} per node, the granularity the
            #                run page wants for progress.
            #   "values"  -> the graph's own state after each step, with
            #                REDUCERS APPLIED.
            #
            # Merging the update deltas ourselves would be wrong for any
            # reducer-backed channel (`add_messages` on MessagesState is
            # the common one): dict.update REPLACES a channel's value
            # where the reducer would have appended, silently dropping
            # accumulated messages. So progress comes from the deltas
            # and the answer comes from LangGraph's own final values.
            async for mode, chunk in graph.astream(
                state, config=config, stream_mode=["updates", "values"]
            ):
                if mode == "values":
                    if _is_a(chunk, dict):
                        final_values = chunk
                    continue
                if not _is_a(chunk, dict):
                    continue
                for node_name, delta in chunk.items():
                    if _is_a(delta, dict):
                        merged_updates.update(delta)
                    # A span here would be a lie. `astream` yields a
                    # node's delta AFTER that node has finished, so a
                    # span opened now would enclose this dict update —
                    # microseconds — while advertising itself as the
                    # node's duration, and the node's own child spans
                    # would sit outside it under the phase span. A
                    # near-zero span labelled with a node's name is
                    # worse than no span: it invites someone to read
                    # timings off it.
                    #
                    # What we can honestly report is that the node
                    # completed, and when. Real per-node spans need
                    # instrumentation around node EXECUTION (LangGraph
                    # callbacks) — a follow-up, recorded in the
                    # blueprint's deviations log rather than faked here.
                    phase_span.add_event(
                        f"node:{_safe_text(node_name)}",
                        attributes={"agent.id": self.agent_id, "node": _safe_text(node_name)},
                    )
                    # Namespaced by phase, because the chassis stores
                    # progress in a Redis hash keyed on step_id alone
                    # (agent_runner.py:206). Node names repeat across
                    # graphs constantly — `agent` and `tools` are the
                    # ReAct convention — so a bare node name would have
                    # the investigate phase overwrite the analyze
                    # phase's entry, and the run page would lose half
                    # its history while every event still looked fine
                    # from inside the adapter.
                    # First run keeps the clean `phase:node` id, so the
                    # ordinary acyclic case reads exactly as before;
                    # repeats are suffixed, so a loop's iterations stay
                    # distinguishable instead of collapsing to one.
                    # `#` is the repeat separator, so it is escaped in
                    # the node name first — otherwise the suffix is not
                    # collision-proof, which defeats the point of having
                    # it. A graph with a node named `work#2` alongside a
                    # node `work` that runs twice emitted `phase:work#2`
                    # for both, and HSET keeps one: measured as
                    # ['analyze:work', 'analyze:work#2', 'analyze:work#2'].
                    # Doubling `#` makes the escape unambiguous and
                    # reversible — `work#2` becomes `work##2`, leaving a
                    # single `#` to mean "run number" and nothing else.
                    safe_node = _safe_text(node_name).replace("#", "##")
                    step_key = f"{phase_name}:{safe_node}"
                    node_runs[step_key] = node_runs.get(step_key, 0) + 1
                    run_index = node_runs[step_key]
                    await on_progress(
                        StepProgress(
                            step_id=(
                                step_key if run_index == 1 else f"{step_key}#{run_index}"
                            ),
                            status="complete",
                        )
                    )

        # Prefer LangGraph's reduced state; fall back to the merged
        # deltas only if a graph produced no values events at all.
        final_state = dict(state)
        final_state.update(final_values if final_values is not None else merged_updates)
        return self._to_result(phase_name, final_state)

    # -- mapping --------------------------------------------------------

    def _to_result(
        self, phase_name: str, state: dict
    ) -> AnalysisResult | InvestigationResult:
        structured = self._extract(state, OUTPUT_KEYS)
        if structured is None:
            # No declared output key: hand back the graph's own state,
            # minus the keys this adapter injected — echoing our own
            # inputs back as the agent's answer would be noise.
            structured = {
                k: v
                for k, v in state.items()
                if k
                not in {
                    STATE_USER_INPUTS,
                    STATE_PRIOR,
                    STATE_EDITS,
                    STATE_RUN_ID,
                    STATE_RUN_ID_LEGACY,
                }
            }
        if not _is_a(structured, dict):
            structured = {"result": structured}

        # Last stop before the chassis: whatever the graph produced has to
        # survive a JSONB write, and the fallback above hands over raw
        # graph state — messages, framework objects, whatever the channels
        # hold. Coercing here rather than at each branch means no path out
        # of this method can return something the runner cannot persist.
        structured = _json_safe(structured)

        # ...and check the shape AGAIN, because the coercion is allowed to
        # change it. `_json_safe` degrades a whole container to a rendered
        # string when something inside refuses conversion — that is its
        # boundary doing its job — so a dict can come back out as a `str`.
        # The runner then evaluates `dict(result.structured or {})`
        # (agent_runner.py:493) and dies on it.
        #
        # Checking before the coercion and trusting the answer afterwards
        # is the same mistake as asking a hostile value twice: the thing
        # being described changed in between.
        if not _is_a(structured, dict):
            structured = {"result": structured}

        if phase_name != self._final_phase:
            return AnalysisResult(display=structured, structured=structured)

        # Repaired on the same terms as `structured`, and for the same
        # reason: the runner assigns this straight to `snap.report_html`,
        # a Text column, which refuses a NUL or an unpaired surrogate
        # exactly as JSONB does. The comment above claimed no path out of
        # this method could return something unpersistable — this one
        # could, because `report_html` is extracted after that coercion
        # rather than through it.
        report_html = self._extract(state, REPORT_KEYS)
        return InvestigationResult(
            status="complete",
            report_html=(
                _pg_safe_text(report_html) if _is_a(report_html, str) else None
            ),
            structured=structured,
        )

    @staticmethod
    def _extract(state: dict, keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in state and state[key] is not None:
                return state[key]
        return None


def capabilities_of(config: Mapping[str, Any] | None) -> Any:
    """The run's capability façade, from a node's ``config`` argument.

    Nodes take ``(state, config)`` and reach the façade through this
    rather than indexing ``config["configurable"]["capabilities"]`` by
    hand — one documented accessor, and it returns ``None`` instead of
    raising when a graph is driven outside a LibreRun run (a unit test,
    a notebook), which is the case a hand-written lookup gets wrong::

        async def gather_context(state, config):
            caps = capabilities_of(config)
            hits = await caps.kb.search(queries=[...], top_k=3)
    """
    if not isinstance(config, Mapping):
        return None
    configurable = config.get("configurable")
    if not isinstance(configurable, Mapping):
        return None
    return configurable.get(CONFIG_CAPABILITIES)


__all__ = [
    "LangGraphAgent",
    "capabilities_of",
    "CONFIG_CAPABILITIES",
    "STATE_USER_INPUTS",
]
