"""The battery against the real LangGraph adapter and example (B16).

B16's accept criterion in executable form: the example adapter agent
must pass the same battery every future adapter will face — plus the
adapter-specific regressions (reducer channels, checkpointers, the
capability façade, JSON-safe output).

This module skips without LangGraph, which is an adapter-side
dependency the chassis deliberately does not ship. The
framework-agnostic battery invariants live in ``test_adapter_kit.py``
and must NOT be moved here: they have to keep running in an
installation that has no adapter framework at all.
"""
from __future__ import annotations

import operator
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, TypedDict

import pytest

from adapter_kit import Scenario, run_battery
from app.agents.protocol import AgentInput, AgentProtocol, AnalysisResult

langgraph = pytest.importorskip(
    "langgraph",
    reason="langgraph is an adapter-side dependency; the chassis does not ship it",
)

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "agents" / "_examples" / "langgraph_triage"


@pytest.fixture(scope="module", autouse=True)
def _tracing_initialized():
    """Initialize tracing for this module — see the sibling module."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())


class AccumulatingState(TypedDict, total=False):
    """State with a reducer-backed channel — module scope so LangGraph can
    resolve the postponed annotations against module globals."""

    user_inputs: dict
    prior_analysis: dict | None
    user_edits: str | None
    capabilities: object
    run_id: str
    messages: Annotated[list, operator.add]


def _example_agent():
    import sys

    sys.path.insert(0, str(EXAMPLE_DIR.parent))
    from langgraph_triage.agent import LangGraphTriageAgent

    return LangGraphTriageAgent()


def _scenario() -> Scenario:
    return Scenario.from_file(EXAMPLE_DIR / "scenarios" / "demo-triage.json")


@pytest.mark.asyncio
async def test_langgraph_example_passes_the_battery():
    """The headline: a LangGraph graph, unmodified, satisfies the platform's
    three promises — schema-valid output, streamed progress, emitted traces."""
    # output_mode mirrors the example's own agent.yaml, so the headline
    # test exercises the same declaration a real deployment renders from.
    result = await run_battery(
        _example_agent(),
        _scenario(),
        phases=["analyze", "investigate"],
        output_mode="structured",
    )
    assert result.passed, result.summary()

    # Both declared phases ran, and each graph node reported progress —
    # four nodes across the two graphs.
    assert result.phases_run == ["analyze", "investigate"]
    # Namespaced by phase: the chassis keys progress on step_id alone, so
    # two graphs sharing a node name (`agent`/`tools` are the ReAct
    # convention) would overwrite each other's history.
    node_names = [step for step, _status in result.progress]
    assert node_names == [
        "analyze:classify",
        "analyze:summarise",
        "investigate:gather_context",
        "investigate:draft",
    ]
    assert all(status == "complete" for _step, status in result.progress)

    # The adapter wrapped each phase in a span, and recorded every node
    # completion as an event on it. Events rather than spans because the
    # stream reports a node only once it has finished — see the adapter's
    # comment; a span opened then would advertise a duration it never
    # measured.
    assert "langgraph:analyze" in result.span_names
    assert "langgraph:investigate" in result.span_names
    assert "node:classify" in result.event_names
    assert "node:draft" in result.event_names

    # The graph's own output survived the trip intact.
    final = result.outputs["investigate"]
    assert final["severity"] == "critical"  # "down" appears in the report
    assert final["next_steps"]


@pytest.mark.asyncio
async def test_reducer_channels_survive_the_adapter():
    """P1: a graph whose channel has a reducer must keep the REDUCED value.

    The adapter used to merge the per-node `updates` deltas itself with
    dict.update, which REPLACES a channel where the reducer would have
    appended — silently dropping accumulated messages on the very common
    MessagesState/add_messages shape. Two nodes each append one item; a
    correct adapter reports both.
    """
    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import LangGraphAgent

    def first(state):
        return {"messages": ["from-first"]}

    def second(state):
        return {"messages": ["from-second"]}

    g = StateGraph(AccumulatingState)
    g.add_node("first", first)
    g.add_node("second", second)
    g.set_entry_point("first")
    g.add_edge("first", "second")
    g.add_edge("second", END)

    agent = LangGraphAgent(
        agent_id="reducer-agent",
        display_name="Reducer",
        description="Appends to a reducer-backed channel from two nodes",
        input_schema={"type": "object"},
        graph=g.compile(),
    )

    result = await run_battery(agent, Scenario("reducer", {}))
    assert result.passed, result.summary()
    messages = result.outputs["analyze"]["messages"]
    assert messages == ["from-first", "from-second"], (
        "the reducer's accumulated value was lost — the adapter merged "
        f"deltas itself instead of using LangGraph's reduced state: {messages}"
    )


@pytest.mark.asyncio
async def test_fallback_state_is_json_safe():
    """P1: the whole-state fallback must survive the chassis' JSONB write.

    The most ordinary LangGraph agent there is — a MessagesState graph
    that writes no explicit output key — falls through to the fallback,
    and that state holds ``AIMessage`` objects. The runner assigns them
    to JSONB columns, so an uncoerced fallback fails the run at COMMIT:
    after all the work, with nothing on the run page.
    """
    import json as _json

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import LangGraphAgent

    def respond(state):
        return {"messages": [AIMessage(content="acknowledged")]}

    g = StateGraph(AccumulatingState)
    g.add_node("respond", respond)
    g.set_entry_point("respond")
    g.add_edge("respond", END)

    agent = LangGraphAgent(
        agent_id="messages-agent",
        display_name="Messages",
        description="Writes LangChain message objects and no output key",
        input_schema={"type": "object"},
        graph=g.compile(),
    )

    result = await run_battery(
        agent,
        Scenario("messages", {}),
        capabilities=None,
    )
    assert result.passed, result.summary()

    structured = result.outputs["analyze"]
    # The chassis does exactly this, via asyncpg, into a JSONB column.
    encoded = _json.dumps(structured)

    # Coerced, not dropped: the message survived as data with its fields
    # intact, which is the whole reason for converting rather than
    # rejecting.
    assert "acknowledged" in encoded, structured
    assert isinstance(structured["messages"][0], dict), structured["messages"]

    # And the same holds for a message handed in as input.
    assert _json.dumps(
        LangGraphAgent(
            agent_id="x",
            display_name="x",
            description="x",
            input_schema={"type": "object"},
            graph=g.compile(),
        )._to_result("analyze", {"structured": {"m": HumanMessage(content="hello")}}).structured
    )


def test_json_safe_keeps_both_entries_when_keys_collide():
    """P2: stringifying keys must not silently drop a value.

    `{1: "numeric", "1": "text"}` has two distinct keys with one string
    form, so a dict comprehension keeps one and discards the other. The
    result is perfectly JSON-safe — so the battery passes and the
    chassis persists an answer that is quietly missing a piece, which is
    worse than the encoding error it replaced.
    """
    from adapters.librerun_langgraph import _json_safe

    out = _json_safe({1: "numeric", "1": "text"})
    assert len(out) == 2, out
    assert set(out.values()) == {"numeric", "text"}, out

    # The uncontested key keeps its natural name; only the colliding one
    # is qualified, so ordinary output is untouched.
    assert out["1"] == "numeric"
    assert out["1#str"] == "text"

    # Three-way collisions still terminate and lose nothing.
    out3 = _json_safe({1: "a", "1": "b", 1.0: "c"})
    assert len(set(out3.values())) == len(out3), out3

    # And the ordinary case is unchanged.
    assert _json_safe({"a": 1, "b": 2}) == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_cyclic_graph_reports_every_iteration():
    """P2: a loop's iterations must not collapse into one progress entry.

    Cyclic graphs are the norm, not the exception — the ReAct pattern
    runs `agent` and `tools` repeatedly — and the chassis writes progress
    with HSET keyed on step_id, so identical ids overwrite. The run page
    would show one completion for a node that ran three times.
    """
    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import LangGraphAgent

    def work(state):
        return {"messages": ["tick"]}

    def decide(state):
        return "work" if len(state.get("messages") or []) < 3 else END

    g = StateGraph(AccumulatingState)
    g.add_node("work", work)
    g.set_entry_point("work")
    g.add_conditional_edges("work", decide, {"work": "work", END: END})

    agent = LangGraphAgent(
        agent_id="cyclic",
        display_name="Cyclic",
        description="Loops over the same node until a condition holds",
        input_schema={"type": "object"},
        graph=g.compile(),
    )

    result = await run_battery(agent, Scenario("cyclic", {}))
    assert result.passed, result.summary()

    steps = [step for step, _status in result.progress]
    assert len(steps) == 3, steps
    # Every iteration is distinguishable, so none is lost to an HSET
    # overwrite — and the first keeps the clean id, so the ordinary
    # acyclic case is unchanged.
    assert len(set(steps)) == len(steps), steps
    assert steps[0] == "analyze:work"
    assert steps[1] == "analyze:work#2"


@pytest.mark.asyncio
async def test_checkpointed_graph_runs():
    """P2: a graph compiled with a checkpointer needs a thread id.

    LangGraph raises before executing a single node when
    ``configurable.thread_id`` is absent, so an author who added
    persistence to a working graph got a traceback and no output. The
    adapter supplies one per (run, phase).
    """
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import LangGraphAgent

    def work(state):
        return {"messages": ["did the work"]}

    g = StateGraph(AccumulatingState)
    g.add_node("work", work)
    g.set_entry_point("work")
    g.add_edge("work", END)

    agent = LangGraphAgent(
        agent_id="checkpointed",
        display_name="Checkpointed",
        description="Compiled with a checkpointer, as a durable graph is",
        input_schema={"type": "object"},
        graph=g.compile(checkpointer=MemorySaver()),
    )

    result = await run_battery(agent, Scenario("checkpointed", {}))
    assert result.passed, result.summary()
    assert result.outputs["analyze"]["messages"] == ["did the work"]


@pytest.mark.asyncio
async def test_capabilities_are_not_checkpointed():
    """P2: the capability façade must not reach a checkpointer.

    It holds live chassis handles — LangGraph's serializer refuses it
    (`Type is not msgpack serializable: Capabilities`) — so a graph
    compiled with a checkpointer died on its first checkpoint. The
    earlier checkpointer test missed this by passing `capabilities=None`
    while `agent_runner.py` always supplies `for_run(...)`.

    It is also wrong on scope: the façade carries one run's id,
    tenant id and grants, and a checkpoint resumed later must not act
    under a scope captured when it was written.
    """
    from uuid import uuid4 as _uuid4

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import LangGraphAgent
    from app import capabilities as caps_mod

    seen: dict[str, Any] = {}

    def work(state, config):
        # Read the raw path rather than the `capabilities_of` helper, so
        # reverting the state→config move fails this test with the real
        # defect (`Type is not msgpack serializable: Capabilities`)
        # rather than an ImportError for the helper.
        seen["caps"] = (config or {}).get("configurable", {}).get("capabilities")
        return {"messages": ["did the work"]}

    g = StateGraph(AccumulatingState)
    g.add_node("work", work)
    g.set_entry_point("work")
    g.add_edge("work", END)

    agent = LangGraphAgent(
        agent_id="cp-caps",
        display_name="Checkpointed with capabilities",
        description="What agent_runner actually constructs",
        input_schema={"type": "object"},
        graph=g.compile(checkpointer=MemorySaver()),
    )

    # The real façade the runner builds, not a stand-in.
    caps = caps_mod.for_run(
        run_id=_uuid4(), tenant_id=_uuid4(), agent_id="probe-agent", grants=["kb"]
    )

    result = await run_battery(agent, Scenario("cp-caps", {}), capabilities=caps)
    assert result.passed, result.summary()

    # The node received it...
    assert seen["caps"] is caps
    # ...and it never entered the state the checkpointer serializes.
    assert "capabilities" not in result.outputs["analyze"], result.outputs["analyze"]


@pytest.mark.asyncio
async def test_example_state_declares_every_injected_key():
    """P2: LangGraph silently drops undeclared state keys.

    The quickstart advertises `user_edits`, so a graph that omits it from
    its schema leaves nodes unable to see the edit a user typed — with no
    error anywhere. Verified against LangGraph's real filtering rather
    than by reading the TypedDict.
    """
    import sys

    sys.path.insert(0, str(EXAMPLE_DIR.parent))
    from langgraph_triage.agent import TriageState, _triage_graph

    from adapters.librerun_langgraph import (
        STATE_RUN_ID,
        STATE_EDITS,
        STATE_PRIOR,
        STATE_USER_INPUTS,
    )

    for key in (STATE_USER_INPUTS, STATE_PRIOR, STATE_EDITS, STATE_RUN_ID):
        assert key in TriageState.__annotations__, (
            f"the example's state schema omits {key!r}, which LangGraph will "
            f"silently drop — nodes could never read it"
        )

    # And prove the filtering is real, not a claim about it.
    seen: dict[str, Any] = {}

    async def _probe(state):
        seen["keys"] = sorted(state.keys())
        return {}

    from langgraph.graph import END, StateGraph

    g = StateGraph(TriageState)
    g.add_node("probe", _probe)
    g.set_entry_point("probe")
    g.add_edge("probe", END)
    await g.compile().ainvoke(
        {
            STATE_USER_INPUTS: {},
            STATE_EDITS: "please redo",
            STATE_RUN_ID: "x",
            "undeclared_key": "dropped",
        }
    )
    assert STATE_EDITS in seen["keys"], seen["keys"]
    assert "undeclared_key" not in seen["keys"], (
        f"expected LangGraph to drop undeclared keys: {seen['keys']}"
    )
    assert _triage_graph() is not None


@pytest.mark.asyncio
async def test_example_queries_the_kb_with_the_incident_summary():
    """P2: the example's capability lookup must search for something.

    `summary` is a channel of the ANALYZE graph; gather_context runs in
    the INVESTIGATE graph, which never has one, so reading it from state
    searched the empty string — an advertised capability demo that
    quietly did nothing whenever a KB was configured.
    """

    class _RecordingKB:
        def __init__(self):
            self.queries: list[str] = []

        def available(self) -> bool:
            return True

        async def search(self, queries, top_k=3):
            self.queries.extend(queries)
            return ["kb-hit-1"]

    class _EchoingLLM:
        """Stands in for the gateway the way KEYLESS mode really behaves.

        The stub honours the caller's own `librerun.stub_reply` above any
        reply it would synthesise, so keyless the example gets back the
        fixture it sent. Echoing it here means this test exercises the
        model path — the call is made, the reply is parsed — while the
        assertions below stay about triage rather than about whatever a
        mock was told to say. A double that returned a canned severity of
        its own would be testing the double.
        """

        def __init__(self):
            self.calls: list[tuple[str, dict]] = []

        async def complete(self, step, messages, **kwargs):
            self.calls.append((step, kwargs))
            return {
                "choices": [
                    {"message": {"content": kwargs["librerun"]["stub_reply"]}}
                ],
                # The gateway names the resolved provider in its own
                # envelope, and keyless that is `stub`. A double that
                # echoes the fixture without it models the half of
                # keyless mode that mattered least (Codex P2).
                "librerun": {"provider": "stub", "step_id": step},
            }

    class _Caps:
        """Answers `granted` from a real list, not with a bare True.

        The nodes ask the façade whether a capability is granted before
        they use it, so a double that says yes to everything would make
        that question untestable — and the ungranted path is the one the
        example promises degrades rather than raises.
        """

        def __init__(self, kb, llm, grants=("kb", "llm")):
            self.kb = kb
            self.llm = llm
            self._grants = frozenset(grants)

        def granted(self, name):
            return name in self._grants

    kb = _RecordingKB()
    llm = _EchoingLLM()
    result = await run_battery(
        _example_agent(),
        _scenario(),
        phases=["analyze", "investigate"],
        capabilities=_Caps(kb, llm),
    )
    assert result.passed, result.summary()

    # Both model steps were really called, by the ids the manifest
    # declares — a step the gateway does not know is a 400.
    assert [step for step, _ in llm.calls] == ["classify", "draft"], llm.calls
    # And the run says a fixture answered, not a model: this double is
    # keyless, and so is the demo this battery stands in for.
    assert result.outputs["investigate"]["severity_source"] == "stub-fixture"
    assert result.outputs["investigate"]["next_steps_source"] == "stub-fixture"

    assert kb.queries, "the knowledge base was never queried"
    assert kb.queries[0], "the example searched the empty string"
    assert kb.queries[0].startswith("checkout-api:"), kb.queries[0]
    assert "triaged critical" in kb.queries[0], kb.queries[0]

    # And the hit reached the output, so the lookup is not decorative.
    assert result.outputs["investigate"]["context"] == ["kb-hit-1"]


def test_capabilities_of_reads_the_documented_path():
    """The accessor nodes are told to use, including the cases a
    hand-written `config["configurable"]["capabilities"]` gets wrong."""
    from adapters.librerun_langgraph import LangGraphAgent, capabilities_of

    sentinel = object()
    assert capabilities_of({"configurable": {"capabilities": sentinel}}) is sentinel

    # Driven outside a LibreRun run — a unit test, a notebook — the
    # helper returns None where indexing would raise.
    assert capabilities_of(None) is None
    assert capabilities_of({}) is None
    assert capabilities_of({"configurable": None}) is None
    assert capabilities_of({"configurable": {}}) is None


def test_json_safe_repairs_strings_postgres_cannot_store():
    """P2: the adapter's "JSON-safe" promise did not cover storability.

    A node returning a string with a NUL or an unpaired surrogate took
    the fast path unchanged, `_to_result()` treated it as safe, and the
    chassis then failed the COMMIT with the graph's work already done.
    The battery catches this for tested scenarios only — production
    inputs produce it just as easily, so the adapter has to repair it.

    Repaired, not rejected: each offending character becomes its literal
    escape text, so nothing is dropped and the reason is visible — the
    same rule this module already applies to colliding mapping keys.
    """
    from librerun_langgraph import _json_safe

    nul = chr(0)
    out = _json_safe({"text": "before" + nul + "after"})
    assert out == {"text": "before\\u0000after"}
    out["text"].encode("utf-8")  # storable

    surrogate = _json_safe({"text": "lead\ud800tail"})
    assert surrogate["text"] == "lead\\ud800tail"
    surrogate["text"].encode("utf-8")  # storable

    # Keys too — a key is as unstorable as a value.
    keyed = _json_safe({"bad" + nul + "key": "v"})
    assert list(keyed) == ["bad\\u0000key"]
    for name in keyed:
        name.encode("utf-8")

    # And nothing else is touched: the round-24 sweep showed ordinary
    # control characters and astral codepoints are perfectly storable.
    untouched = {"c": chr(1) + chr(7) + chr(127) + "\U0001F600" + "\n"}
    assert _json_safe(untouched) == untouched


def test_every_string_leaving_json_safe_is_storable():
    """P2 x3: the repair belonged at the exit, not at three call sites.

    `_json_safe` returns strings from eight branches. Round 25 fixed the
    string fast path and the mapping key; round 26 found decoded bytes,
    the collision-suffix key, and `report_html` still raw. Repairing at
    the single exit is what stops a ninth branch reintroducing it.
    """
    from librerun_langgraph import _json_safe

    nul = chr(0)

    # 1. decoded bytes (round-26 finding)
    assert _json_safe({"b": ("log" + nul + "tail").encode()}) == {
        "b": "log\\u0000tail"
    }

    # 2. the collision-suffix key path, which interpolated the RAW key
    collided = _json_safe({"k" + nul: "by-str", ("k" + nul,): "by-tuple"})
    for name in collided:
        assert nul not in name, f"unrepaired key: {name!r}"
        name.encode("utf-8")
    assert len(collided) == 2, f"a value was dropped: {collided}"

    # 3. an object whose repr carries one
    class Nasty:
        def __repr__(self) -> str:
            return "obj" + nul + "repr"

    assert _json_safe({"o": Nasty()}) == {"o": "obj\\u0000repr"}

    # Storable in every case.
    for payload in (
        _json_safe({"b": ("x" + nul).encode()}),
        _json_safe({"o": Nasty()}),
    ):
        for v in payload.values():
            v.encode("utf-8")


def test_report_html_is_repaired_before_it_reaches_the_chassis():
    """P2: `report_html` skipped the coercion `structured` goes through.

    `_to_result` coerces `structured` and then extracts `report_html`
    AFTER it — so the comment claiming no path out of the method could
    return something unpersistable was false for exactly this field. The
    runner assigns it straight to `snap.report_html`, a Text column,
    which refuses the same two characters JSONB does.
    """
    from adapters.librerun_langgraph import LangGraphAgent
    from langgraph.graph import END, StateGraph

    nul = chr(0)

    def noop(state):
        return {}

    g = StateGraph(AccumulatingState)
    g.add_node("noop", noop)
    g.set_entry_point("noop")
    g.add_edge("noop", END)

    agent = LangGraphAgent(
        agent_id="report-nul",
        display_name="Report NUL",
        description="Final state carries an unstorable report",
        input_schema={"type": "object"},
        graph=g.compile(),
    )

    out = agent._to_result("analyze", {"report_html": "<p>ok" + nul + "</p>"})
    assert out.report_html == "<p>ok\\u0000</p>", out.report_html
    out.report_html.encode("utf-8")  # storable

    # A clean report is passed through untouched.
    fine = agent._to_result("analyze", {"report_html": "<p>fine</p>"})
    assert fine.report_html == "<p>fine</p>"


def test_oversized_integers_are_coerced():
    """P2: a valid Python int `json.dumps` refuses.

    CPython caps int-to-string conversion at 4300 digits, so `10**5000`
    raises during the runner's JSONB write — after the graph finished.
    The integer twin of the NaN case beside it.

    `str()` on such an int raises too, so the replacement must avoid
    decimal: `bit_length()` and hex formatting are exempt from the cap.
    """
    import json as _json

    from librerun_langgraph import _json_safe

    big = 10 ** 5000
    with pytest.raises(ValueError):
        _json.dumps({"n": big})  # the premise

    out = _json_safe({"n": big})
    assert out["n"].startswith("<int too large to serialize: 16610 bits, 0x")
    _json.dumps(out)  # storable

    # Ordinary integers are untouched — the over-strict direction would
    # be stringifying every int.
    assert _json_safe({"n": 42, "big_but_fine": 10**100}) == {
        "n": 42,
        "big_but_fine": 10**100,
    }


@pytest.mark.asyncio
async def test_repeated_node_ids_cannot_collide_with_a_literal_node_name():
    """P2: the repeat suffix was not collision-proof.

    A graph with a node literally named `work#2`, alongside a `work`
    that runs twice, emitted `phase:work#2` for BOTH. The chassis stores
    progress with HSET keyed on step_id, so one completion overwrote the
    other — exactly what the suffix was added to prevent.

    Driven through the real adapter rather than a reimplementation of
    its id scheme: a test that recomputes the logic it is checking would
    pass whatever the adapter does, which is the defect this whole batch
    is about.
    """
    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import LangGraphAgent

    def work(state):
        return {"messages": ["tick"]}

    def hashed(state):
        return {"messages": ["hash"]}

    def decide(state):
        # work -> work -> work#2 -> END
        return "work" if len(state.get("messages") or []) < 2 else "work#2"

    g = StateGraph(AccumulatingState)
    g.add_node("work", work)
    g.add_node("work#2", hashed)
    g.set_entry_point("work")
    g.add_conditional_edges("work", decide, {"work": "work", "work#2": "work#2"})
    g.add_edge("work#2", END)

    agent = LangGraphAgent(
        agent_id="hash-collision",
        display_name="Hash collision",
        description="A node named work#2 beside a work that runs twice",
        input_schema={"type": "object"},
        graph=g.compile(),
    )

    result = await run_battery(agent, Scenario("collide", {}))
    steps = [step for step, _status in result.progress]

    assert len(set(steps)) == len(steps), f"HSET would lose one of: {steps}"
    # The node literally named `work#2` escapes to `work##2`, leaving a
    # single `#` to mean "run number" and nothing else.
    assert "analyze:work" in steps, steps
    assert "analyze:work#2" in steps, steps      # second run of `work`
    assert "analyze:work##2" in steps, steps     # the node named work#2


def test_oversized_integer_mapping_keys_are_coerced():
    """P2: `str(key)` raised before the key could be sanitized.

    The value path was repaired in round 27 and the KEY path was not —
    `str(key)` on `10**5000` raises, so `_to_result()` failed after the
    graph had completed and the runner had nothing to persist.
    """
    import json as _json

    from librerun_langgraph import _json_safe

    big = 10 ** 5000
    with pytest.raises(ValueError):
        str(big)  # the premise

    out = _json_safe({big: "v"})
    name = next(iter(out))
    assert name.startswith("<int too large to serialize: 16610 bits, 0x")
    assert out[name] == "v"
    _json.dumps(out)  # storable

    # And an oversized int nested past the depth cutoff, where the
    # `repr` fallback runs BEFORE the integer branch could coerce it.
    deep: Any = big
    for _ in range(15):
        deep = {"n": deep}
    _json.dumps(_json_safe(deep))


def test_conversion_survives_state_whose_own_code_raises():
    """P2: the "safe" helpers guarded `ValueError` and nothing else.

    Round 28 made `_safe_text`/`_safe_repr` survive an oversized int,
    because `ValueError` is what CPython's digit cap raises. But every
    one of these helpers runs the GRAPH'S code — `__str__`, `__repr__`,
    `isoformat`, `type(x).__name__` — and a node's object may raise
    anything at all, or return a non-string, which makes the builtin
    raise `TypeError`. So the guard covered the reported symptom and
    left the mechanism: `_to_result()` still died on the way to the
    database, with the graph's work already done.
    """
    import json as _json
    from datetime import datetime

    # Only the pre-existing names here: importing a helper this round
    # ADDS would make the whole test fail on ImportError against the
    # unfixed code, hiding whether the behaviour below actually
    # regressed. `_safe_type_name` is imported at its use instead.
    from librerun_langgraph import _json_safe, _safe_repr, _safe_text

    class Hostile:
        """Not exotic: an object whose `__repr__` reaches for a lazily
        loaded attribute that is not there yet does exactly this."""

        def __repr__(self):
            raise RuntimeError("repr unavailable")

        def __str__(self):
            raise RuntimeError("str unavailable")

    class WrongType:
        """`__repr__` returning a non-string — a `TypeError` from the
        builtin rather than from the object, and equally fatal."""

        def __repr__(self):
            return 42

        def __str__(self):
            return 42

    # The premise: both defeat the builtins outright.
    for bad in (Hostile(), WrongType()):
        with pytest.raises((RuntimeError, TypeError)):
            repr(bad)
        with pytest.raises((RuntimeError, TypeError)):
            str(bad)

    assert _safe_repr(Hostile()) == "<unrepresentable Hostile>"
    assert _safe_text(Hostile()) == "<unrepresentable Hostile>"
    assert _safe_repr(WrongType()) == "<unrepresentable WrongType>"

    # As a VALUE: the terminal fallback of the conversion.
    out = _json_safe({"node_output": Hostile()})
    assert out["node_output"] == "<unrepresentable Hostile>"
    _json.dumps(out)  # storable

    # As a KEY: `_safe_text(key)` runs before anything can sanitize it.
    out = _json_safe({Hostile(): "v"})
    assert out == {"<unrepresentable Hostile>": "v"}
    _json.dumps(out)

    # Two hostile keys collide, and the disambiguating suffix is built
    # from `type(key).__name__` — which a metaclass also controls.
    out = _json_safe({Hostile(): "a", WrongType(): "b"})
    assert len(out) == 2, out
    _json.dumps(out)

    # `isoformat` is as overridable as `__str__`, and the datetime
    # branch called it bare.
    class HostileStamp(datetime):
        def isoformat(self, *a, **k):
            raise RuntimeError("no isoformat")

        def __repr__(self):
            return "<HostileStamp>"

    assert _json_safe(HostileStamp(2026, 1, 1)) == "<HostileStamp>"

    # A float/Decimal/UUID subclass reaches `str()` on the same terms.
    class HostileFloat(float):
        def __str__(self):
            raise RuntimeError("no str")

    # `"nan"`, not `"<unrepresentable HostileFloat>"`: `_safe_text` now
    # tries `_safe_repr` before giving up, and `float.__repr__` works
    # perfectly well here. That makes a hostile NaN render exactly like
    # a plain one — losing the value when a working avenue remained was
    # the weaker answer.
    assert _json_safe(float("nan")) == "nan"
    assert _json_safe(HostileFloat("nan")) == "nan"
    _json.dumps(_json_safe({"f": HostileFloat("inf")}))

    # The last assumption every fallback above makes — a metaclass owns
    # `__name__` as surely as a class owns `__repr__`.
    from librerun_langgraph import _safe_type_name

    assert _safe_type_name(Hostile()) == "Hostile"

    class _NoName(type):
        @property
        def __name__(cls):  # noqa: N805 - deliberately hostile
            raise RuntimeError("no name")

    class _Nameless(metaclass=_NoName):
        pass

    assert _safe_type_name(_Nameless()) == "?"
    assert _json_safe({"x": _Nameless()}) != {}


def test_int_conversion_probes_what_json_actually_calls():
    """The int branch probed with `str()`, which is not what json uses.

    `json.dumps` encodes an int through `int.__repr__`, never through
    `__str__` — so the probe asked a neighbouring question, and asked it
    through a method an `int` subclass can override to raise. A subclass
    with a hostile `__str__` is perfectly storable and was being killed
    by the check meant to protect it.
    """
    import json as _json

    from librerun_langgraph import _json_safe

    class HostileStr(int):
        def __str__(self):
            raise RuntimeError("no str for you")

    value = HostileStr(42)
    with pytest.raises(RuntimeError):
        str(value)  # the premise
    assert _json.dumps(value) == "42"  # yet json is perfectly happy

    assert _json_safe(value) == 42
    _json.dumps(_json_safe({"n": value}))

    # The oversized case the previous round added still behaves.
    out = _json_safe(10 ** 5000)
    assert out.startswith("<int too large to serialize: 16610 bits, 0x")


def test_node_names_are_rendered_safely_too():
    """Node names come off the LangGraph stream and are `str()`-wrapped.

    The wrapping is itself the admission that they may not be strings —
    and a value that needs `str()` is a value whose `__str__` is the
    graph author's code. Three sites: the span name, the `node`
    attribute, and the step id's `#` escaping.
    """
    from librerun_langgraph import _safe_text

    class HostileName:
        def __str__(self):
            raise RuntimeError("no str")

        def __repr__(self):
            raise RuntimeError("no repr either")

    assert _safe_text(HostileName()) == "<unrepresentable HostileName>"
    # The step-id escaping runs on the result, so it must be a real str.
    assert _safe_text(HostileName()).replace("#", "##") == "<unrepresentable HostileName>"


class HostileStr(str):
    """A `str` subclass that owns every method the sanitizer calls.

    Not a thought experiment: `SecretStr`-style wrappers, lazy i18n
    proxies and templating libraries all ship `str` subclasses, and one
    that raises on an unexpected `encode(...)` signature behaves exactly
    like this.
    """

    def encode(self, *a, **k):
        raise RuntimeError("encode is not available")

    def replace(self, *a, **k):
        raise RuntimeError("replace is not available")

    def __contains__(self, item):
        raise RuntimeError("contains is not available")

    def __format__(self, spec):
        raise RuntimeError("format is not available")


def test_str_subclasses_are_normalized_before_their_methods_run():
    """P2: `isinstance(x, str)` is a licence to call string methods.

    A `str` subclass passes that check and owns `encode`, `replace`,
    `__contains__` and `__format__` — all of which `_pg_safe_text`
    invokes. And this one inverts the usual direction: `json.dumps`
    reads the underlying buffer and accepts the value happily, so the
    sanitizer that exists to SAVE the run was the only thing killing it.
    Every other guard here protects the run from an unstorable value;
    this one protects a storable value from us.
    """
    import json as _json

    from librerun_langgraph import _json_safe

    hostile = HostileStr("ok\x00bad")

    # The premise, on record: production would have stored this.
    assert _json.dumps(hostile) == '"ok\\u0000bad"'
    with pytest.raises(RuntimeError):
        hostile.encode("utf-8")

    # As a value — and the NUL is still repaired, so normalizing did not
    # cost the sanitization it was blocking.
    out = _json_safe({"answer": hostile})
    assert out == {"answer": "ok\\u0000bad"}
    assert type(out["answer"]) is str
    _json.dumps(out)

    # As a mapping key, where `_safe_text` returns it before
    # `_pg_safe_text` ever sees it.
    out = _json_safe({hostile: "v"})
    assert out == {"ok\\u0000bad": "v"}
    assert type(next(iter(out))) is str

    # Bare, through the wrapper's own string exit.
    assert type(_json_safe(hostile)) is str

    # An object that LIES about `__class__` passes `isinstance` and is
    # not a string at all — `json.dumps` refuses it, so degrading to a
    # repr is the right answer rather than a loss.
    class Liar:
        @property
        def __class__(self):
            return str

        def __repr__(self):
            return "<Liar>"

    assert isinstance(Liar(), str)
    with pytest.raises(TypeError):
        _json.dumps(Liar())
    _json.dumps(_json_safe({"x": Liar()}))


@pytest.mark.asyncio
async def test_a_graph_returning_a_str_subclass_still_completes():
    """The finding driven through a real graph, not through the helper.

    A node writing a `str` subclass into state is the actual reported
    failure: `_to_result()` raised after the graph had finished, so the
    run died with the work already done.
    """
    from langgraph.graph import END, StateGraph

    from adapter_kit import Scenario, run_battery
    from librerun_langgraph import LangGraphAgent

    class State(TypedDict, total=False):
        structured: dict
        report_html: str

    def work(state: State) -> State:
        return {
            "structured": {"finding": HostileStr("subclassed\x00answer")},
            "report_html": HostileStr("<p>report</p>"),
        }

    g = StateGraph(State)
    g.add_node("work", work)
    g.set_entry_point("work")
    g.add_edge("work", END)

    agent = LangGraphAgent(
        agent_id="subclass-state",
        display_name="Subclass state",
        description="A node whose output is a str subclass",
        input_schema={"type": "object"},
        graph=g.compile(),
    )

    result = await run_battery(agent, Scenario("subclass", {}))
    assert result.passed, result.summary()
    assert result.outputs["analyze"] == {"finding": "subclassed\\u0000answer"}


def test_the_repr_fallback_is_itself_normalized():
    """P2: `repr()` accepts a `str` SUBCLASS as its return value.

    So `_exact_str`'s own fallback — reached when `__class__` lies and
    the base slot refuses the object — was handing back exactly what the
    guard exists to remove, and `_pg_safe_text` then ran the subclass's
    `__contains__`. A guard whose fallback reintroduces the hazard is
    not a guard.
    """
    import json as _json

    from librerun_langgraph import _exact_str, _json_safe

    class ReprLiar:
        """Lies about `__class__` AND returns a hostile subclass."""

        @property
        def __class__(self):
            return str

        def __repr__(self):
            return HostileStr("looks fine")

    liar = ReprLiar()

    # The premise, measured: `repr()` does NOT normalize its result.
    assert type(repr(liar)) is HostileStr
    with pytest.raises(RuntimeError):
        "\x00" in repr(liar)

    out = _exact_str(liar)
    assert type(out) is str, type(out)
    assert out == "looks fine"

    # And end to end, where the failure actually landed.
    assert isinstance(liar, str)  # the trapdoor `isinstance` opens
    _json.dumps(_json_safe({"answer": liar}))
    _json.dumps(_json_safe({liar: "v"}))


def test_a_bool_liar_is_not_waved_through_the_fast_path():
    """P2: `isinstance(x, bool)` is not "is a bool".

    `bool` cannot be subclassed, so anything passing that check without
    being one is an object lying through `__class__` — and the
    JSON-native fast path returned it unchanged, handing the runner a
    value `json.dumps` refuses after the graph had finished. The string
    path already drew this distinction; this one did not.

    `bool` is also a subclass of `int`, so the same object lands in the
    integer branch, where `int.__repr__` rejects it with `TypeError` —
    which that branch's `except ValueError` did not catch either.
    """
    import json as _json

    from librerun_langgraph import _json_safe

    class BoolLiar:
        @property
        def __class__(self):
            return bool

        def __repr__(self):
            return "<BoolLiar>"

    liar = BoolLiar()
    # The premise: it passes both checks and json refuses it.
    assert isinstance(liar, bool)
    assert isinstance(liar, int)  # bool is a subclass of int
    with pytest.raises(TypeError):
        _json.dumps(liar)

    out = _json_safe({"flag": liar})
    assert out == {"flag": "<BoolLiar>"}
    _json.dumps(out)  # storable

    # Real booleans are untouched.
    assert _json_safe({"t": True, "f": False}) == {"t": True, "f": False}
    assert _json_safe(True) is True


def test_every_scalar_branch_survives_a_class_that_lies():
    """P2 (x2): `isinstance` only consults `__class__`.

    So every `isinstance` branch in `_json_safe_value` is reachable by
    an object that is not the type it claims, and each branch's own
    operation then raises: `math.isfinite` wants a real number,
    `bytes()` wants real bytes, `int.__repr__` is bound to `int`.

    Three separate rounds patched three separate branches (`bool`, then
    `float`, then `bytes`). This asserts the whole set at once, because
    the fix is now a single guard around the scalar conversion rather
    than a per-branch catch — a fourth lied-about type must not reopen
    it.
    """
    import json as _json

    from librerun_langgraph import _json_safe

    def liar(claimed, label):
        class Liar:
            @property
            def __class__(self):
                return claimed

            def __repr__(self):
                return f"<{label}>"

        return Liar()

    for claimed, label in (
        (bool, "BoolLiar"),
        (int, "IntLiar"),
        (float, "FloatLiar"),
        (bytes, "BytesLiar"),
        (str, "StrLiar"),
    ):
        value = liar(claimed, label)
        assert isinstance(value, claimed)          # the trapdoor
        out = _json_safe({"v": value})
        assert out == {"v": f"<{label}>"}, (label, out)
        _json.dumps(out)                            # storable

    # A REAL bytes subclass whose `__bytes__` raises — not a liar, an
    # actual subclass, so the branch is entered legitimately and the
    # conversion is what fails.
    class HostileBytes(bytes):
        def __bytes__(self):
            raise RuntimeError("no bytes for you")

    out = _json_safe({"b": HostileBytes(b"\xff\xfe")})
    assert isinstance(out["b"], str)
    _json.dumps(out)

    # And the ordinary cases still convert, since the risk in guarding a
    # fast path is over-converting what was fine.
    assert _json_safe({"i": 7, "f": 1.5, "t": True, "n": None}) == {
        "i": 7,
        "f": 1.5,
        "t": True,
        "n": None,
    }
    assert _json_safe(float("nan")) == "nan"
    assert _json_safe(b"ok") == "ok"


def test_structural_branches_survive_a_class_that_lies_too():
    """P2: guarding the scalars and leaving the containers was half a fix.

    `isinstance(x, dict)` and the `Mapping`/`Sequence` ABC checks are
    satisfied by anything whose `__class__` says so — after which
    `.items()`, iteration, `.value` or `asdict()` raises. The previous
    round put a boundary around the scalar conversions; the very next
    lied-about type simply landed one branch further down.

    The whole conversion now has ONE boundary, so this asserts the
    structural branches as a set rather than the one review named.
    """
    import json as _json
    from dataclasses import dataclass as _dataclass

    from librerun_langgraph import _json_safe

    def liar(claimed, label):
        class Liar:
            @property
            def __class__(self):
                return claimed

            def __repr__(self):
                return f"<{label}>"

        return Liar()

    for claimed, label in ((dict, "DictLiar"), (list, "ListLiar"), (tuple, "TupleLiar")):
        value = liar(claimed, label)
        assert isinstance(value, claimed)          # the trapdoor
        out = _json_safe({"v": value})
        assert out == {"v": f"<{label}>"}, (label, out)
        _json.dumps(out)

    # A REAL Mapping whose `items()` raises — entered legitimately, and
    # the conversion is what fails.
    class HostileMapping(dict):
        def items(self):
            raise RuntimeError("items is not available")

    out = _json_safe({"m": HostileMapping(a=1)})
    assert isinstance(out["m"], str)
    _json.dumps(out)

    # An `Enum` whose `.value` raises, and a dataclass `asdict` cannot
    # walk — the other two structural branches.
    class HostileEnumLike:
        @property
        def __class__(self):
            return Enum

        def __repr__(self):
            return "<EnumLiar>"

    assert _json_safe({"e": HostileEnumLike()}) == {"e": "<EnumLiar>"}

    # And the ordinary containers still convert, since the risk in
    # adding a boundary is swallowing the cases that were working.
    assert _json_safe({"d": {"k": 1}, "l": [1, 2], "t": (3, 4)}) == {
        "d": {"k": 1},
        "l": [1, 2],
        "t": [3, 4],
    }

    @_dataclass
    class Point:
        x: int
        y: int

    assert _json_safe(Point(1, 2)) == {"x": 1, "y": 2}


@pytest.mark.asyncio
async def test_a_state_value_with_a_hostile_class_does_not_escape_the_adapter():
    """P2: `_to_result` ran a bare `isinstance` on graph-written state.

    ``isinstance`` consults ``__class__``, which a graph-written value
    owns. The wrapping branch and the ``_json_safe`` coercion after it
    are the adapter's entire reason for existing — turning arbitrary
    graph output into something the chassis can persist — and a raising
    ``__class__`` escaped before either was reached, after the graph had
    already done all of its work. The runner then marks the phase
    errored rather than degrading the value.
    """
    from langgraph.graph import END, StateGraph

    from librerun_langgraph import LangGraphAgent

    class LyingClass:
        @property
        def __class__(self):
            raise RuntimeError("class is not available")

    class State(TypedDict, total=False):
        user_inputs: dict
        structured: Any

    def node(state):
        return {"structured": LyingClass()}

    builder = StateGraph(State)
    builder.add_node("work", node)
    builder.set_entry_point("work")
    builder.add_edge("work", END)
    graph = builder.compile()

    agent = LangGraphAgent(
        agent_id="hostile-class",
        display_name="Hostile class",
        description="Writes a state value whose __class__ refuses to be read",
        input_schema={"type": "object"},
        graph=graph,
    )

    result = await run_battery(
        agent, Scenario("hostile-class", {}), require_traces=False
    )

    # The point is that the adapter DEGRADED it rather than escaping:
    # run_battery returned a verdict at all, and the output is a dict the
    # chassis could persist.
    assert isinstance(result.outputs.get("analyze"), dict), result.outputs


@pytest.mark.asyncio
async def test_legacy_case_id_state_channel_still_receives_the_run_id_for_one_release():
    """Blueprint S1 (L18), Codex finding on PR #49: LangGraph drops input
    keys a graph's schema does not declare, so a graph written against the
    earlier contract — `case_id` in its state — would have lost the id
    once the adapter injected only `run_id`. Both keys are injected for
    one release; a graph declaring either one sees the id.
    """
    from typing import TypedDict
    from uuid import uuid4 as _uuid4

    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import LangGraphAgent

    class _LegacyState(TypedDict, total=False):
        case_id: str
        result: dict

    class _CurrentState(TypedDict, total=False):
        run_id: str
        result: dict

    def legacy_node(state):
        return {"result": {"seen": state.get("case_id")}}

    def current_node(state):
        return {"result": {"seen": state.get("run_id")}}

    for schema, node, label in (
        (_LegacyState, legacy_node, "legacy-case-id"),
        (_CurrentState, current_node, "current-run-id"),
    ):
        g = StateGraph(schema)
        g.add_node("n", node)
        g.set_entry_point("n")
        g.add_edge("n", END)
        agent = LangGraphAgent(
            agent_id=label,
            display_name=label,
            description="reads the run id from its own declared channel",
            input_schema={"type": "object"},
            graph=g.compile(),
        )
        run_id = _uuid4()
        result = await run_battery(agent, Scenario(label, {}), run_id=run_id)
        assert result.passed, result.summary()
        seen = result.outputs["analyze"]["seen"]
        assert seen == str(run_id), (
            f"{label}: the graph's declared channel did not carry the run id: {seen!r}"
        )


@pytest.mark.asyncio
async def test_fallback_output_excludes_both_adapter_owned_id_keys():
    """The whole-state fallback hands back the graph's own data minus what
    the adapter injected — and that now includes the legacy `case_id`
    channel as well as `run_id`, so neither echoes back as the answer."""
    from typing import TypedDict

    from langgraph.graph import END, StateGraph

    from adapters.librerun_langgraph import (
        STATE_RUN_ID,
        STATE_RUN_ID_LEGACY,
        LangGraphAgent,
    )

    class _BothState(TypedDict, total=False):
        run_id: str
        case_id: str
        answer: str

    def node(state):
        assert state.get(STATE_RUN_ID) == state.get(STATE_RUN_ID_LEGACY)
        return {"answer": "42"}

    g = StateGraph(_BothState)
    g.add_node("n", node)
    g.set_entry_point("n")
    g.add_edge("n", END)
    agent = LangGraphAgent(
        agent_id="both-ids",
        display_name="both",
        description="declares both id channels and no output key",
        input_schema={"type": "object"},
        graph=g.compile(),
    )
    result = await run_battery(agent, Scenario("both", {}))
    assert result.passed, result.summary()
    out = result.outputs["analyze"]
    assert out == {"answer": "42"}, f"adapter-owned keys leaked into the output: {out}"
