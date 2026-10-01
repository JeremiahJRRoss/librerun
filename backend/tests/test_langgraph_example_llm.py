"""The LangGraph example really calls a model, and says when it did not
(blueprint S5).

The example used to be deterministic end to end, and its manifest said
so. `llm.steps` in a manifest alone proves nothing: no call is made, so
there is no costed span and nothing for D13 to retarget. Two of its
nodes now go through the in-process `llm` capability, and these tests
pin the three things that are easy to get wrong and invisible when they
are:

* the **step ids** the nodes send are the ids the manifest declares —
  the gateway answers `400 unknown_step` to anything else, and a rename
  in one file only is the way that happens;
* the call carries the agent's **own keyless fixture**, so a keyless run
  shows what the rule says rather than the first member of the schema's
  enum;
* an **ungranted or unreachable** capability degrades to the rule and
  says so in the output, rather than raising, and rather than passing a
  fixture off as a judgement.

They drive the shipped example, not a re-implementation of it: an
assertion about an example is only worth making against the file an
author will copy.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from pathlib import Path

import pytest
import yaml

pytest.importorskip(
    "langgraph",
    reason="langgraph is an adapter-side dependency; the chassis does not ship it",
)

EXAMPLE_DIR = (
    Path(__file__).resolve().parents[1] / "agents" / "_examples" / "langgraph_triage"
)


def _example_module():
    sys.path.insert(0, str(EXAMPLE_DIR.parent))
    from langgraph_triage import agent as module

    return module


def _manifest() -> dict:
    return yaml.safe_load((EXAMPLE_DIR / "agent.yaml").read_text())


QUICKSTART = Path(__file__).resolve().parents[2] / "docs" / "authoring" / "LangGraph.md"


def _nodes_the_graphs_declare() -> set[str]:
    """Node names read out of the `add_node` calls, not listed here.

    A list in this file would be a second copy of the same fact and would
    go stale exactly when the thing it guards does.
    """
    tree = ast.parse((EXAMPLE_DIR / "agent.py").read_text())
    names = set()
    for n in ast.walk(tree):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "add_node"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
        ):
            names.add(n.args[0].value)
    return names


def test_every_node_the_quickstart_shows_is_a_node_the_example_has():
    """The trace in the quickstart must name real nodes.

    Renaming `draft_plan` to `draft` left `node:draft_plan` in the
    quickstart's trace example, where a reader compares the documented
    tree against the one their own run produces and concludes the event
    is missing. That is the second time in this batch that prose outlived
    the code it described — the first was a docstring still teaching a
    reverted join — so the class gets a guard rather than a third
    apology.

    What is pinned is NODE NAMES, which are identifiers, not wording. A
    rule matching prose would pin the phrasing and fail the moment
    someone improves a sentence, which is the failure this batch has
    recorded repeatedly.

    One direction only, deliberately: everything the document shows must
    exist, but the example may have nodes the document does not show —
    the trace illustrates one phase, not the whole agent. The reverse
    direction would force every node into the diagram, which is a
    documentation style decision rather than a correctness property.
    """
    documented = set(re.findall(r"node:([A-Za-z_][A-Za-z0-9_]*)", QUICKSTART.read_text()))
    assert documented, "no `node:` events found — the anchor moved, so this guards nothing"
    real = _nodes_the_graphs_declare()
    assert real, "no add_node calls found — the reader moved, so this guards nothing"
    unknown = documented - real
    assert not unknown, (
        f"the quickstart shows node(s) the example does not have: {sorted(unknown)}. "
        f"Nodes actually declared: {sorted(real)}. Rename the doc or the node."
    )


def _steps_called_in_source() -> set[str]:
    """The step ids the nodes actually pass, read out of the source.

    Read rather than listed, because a list here is a second copy of the
    thing under test: it would keep agreeing with the manifest while the
    code drifted away from both.
    """
    tree = ast.parse((EXAMPLE_DIR / "agent.py").read_text())
    found = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_ask_the_model"
        ):
            # _ask_the_model(config, "<step>", ...) — positional, and a
            # non-literal would be a step no static check could follow,
            # so it fails loudly rather than being skipped.
            step = node.args[1]
            assert isinstance(step, ast.Constant) and isinstance(step.value, str), (
                f"the step id at line {node.lineno} is not a literal, so neither "
                f"this test nor a reader can tell which step is being called"
            )
            found.add(step.value)
    return found


def test_every_step_the_nodes_call_is_declared_in_the_manifest():
    declared = {s["id"] for s in _manifest()["llm"]["steps"]}
    called = _steps_called_in_source()
    assert called, "no model call was found in the example at all"
    assert called == declared, (
        f"called={sorted(called)} declared={sorted(declared)}. The gateway "
        f"refuses a step the manifest does not declare (400 unknown_step) and "
        f"a declared step nothing calls is a model an admin can configure and "
        f"never reach."
    )


def test_the_manifest_grants_llm():
    """Belt and braces with the loader's own `_check_llm_grant`.

    That validator refuses `llm.steps` without the grant at load time,
    which is the stronger guard — but it fires only while steps exist.
    This says the example, specifically, ships both.
    """
    manifest = _manifest()
    assert "llm" in manifest["capabilities"], manifest["capabilities"]
    assert manifest["llm"]["steps"], "the grant without steps calls nothing"


class _RecordingLLM:
    """The gateway, faithfully enough to be worth asserting against.

    Two halves, and the second is the one a careless double drops:

    * keyless it echoes the caller's own `librerun.stub_reply`, which the
      stub honours above anything it would synthesise;
    * and it answers in the gateway's `librerun` envelope, naming the
      PROVIDER that resolved — `stub` keyless. Without that the node
      cannot tell a fixture from a judgement, which was the defect
      (Codex P2), and a double that omitted it would let the defect back
      in while staying green.

    `provider=None` models a gateway that sent no envelope at all.
    """

    def __init__(
        self,
        *,
        raises: Exception | None = None,
        reply: dict | None = None,
        provider: str | None = "stub",
        envelope: bool = True,
    ):
        self.calls: list[tuple[str, list, dict]] = []
        self._raises = raises
        self._reply = reply
        self._provider = provider
        self._envelope = envelope

    async def complete(self, step, messages, **kwargs):
        self.calls.append((step, messages, kwargs))
        if self._raises is not None:
            raise self._raises
        content = (
            json.dumps(self._reply)
            if self._reply is not None
            else kwargs["librerun"]["stub_reply"]
        )
        out = {"choices": [{"message": {"content": content}}]}
        if self._envelope:
            out["librerun"] = {"provider": self._provider, "step_id": step}
        return out


class _Caps:
    def __init__(self, llm, grants=("llm",)):
        self.llm = llm
        self._grants = frozenset(grants)

    def granted(self, name):
        return name in self._grants


class _CancellingKbCaps:
    """`gather_context` reaches `caps.kb`, not `caps.llm`."""

    def __init__(self, kb):
        self.kb = kb

    def granted(self, name):
        return name == "kb"


def _config(caps):
    from adapters.librerun_langgraph import CONFIG_CAPABILITIES

    return {"configurable": {CONFIG_CAPABILITIES: caps}}


REPORT = {
    "user_inputs": {
        "title": "Login is slow for some customers",
        "service": "auth-api",
        "description": "Sign-in is intermittent and retries time out.",
        "logs": "",
    }
}


@pytest.mark.asyncio
async def test_classify_sends_the_step_the_schema_and_its_own_fixture():
    module = _example_module()
    llm = _RecordingLLM()

    await module.classify(REPORT, _config(_Caps(llm)))

    (step, messages, kwargs) = llm.calls[0]
    assert step == "classify"
    # The STEP is the whole request — a model name here would take the
    # choice away from the operator (L25, D13).
    assert "model" not in kwargs, kwargs
    assert kwargs["response_format"]["json_schema"]["name"] == "classify"
    assert (
        kwargs["response_format"]["json_schema"]["schema"]
        == module.CLASSIFY_SCHEMA
    )
    # The agent's own keyless fixture, and it is the RULE's answer — not
    # a canned string that could drift away from what the fallback does.
    fixture = json.loads(kwargs["librerun"]["stub_reply"])
    assert fixture == module._by_the_rules(REPORT)
    # The report reached the model; the prompt is not a stub.
    assert "auth-api" in messages[-1]["content"]


@pytest.mark.asyncio
async def test_keyless_the_agents_own_fixture_is_what_comes_back():
    """The honesty claim: keyless output equals the rule's answer.

    Without `librerun.stub_reply` the gateway would synthesise an
    instance of the schema — valid, deterministic, and always the FIRST
    enum member, so every keyless incident would read `critical`. This
    report is a `medium` one; if the fixture stopped being sent, it would
    stop reading `medium`.
    """
    module = _example_module()
    out = await module.classify(REPORT, _config(_Caps(_RecordingLLM())))

    rules = module._by_the_rules(REPORT)
    assert rules["severity"] == "medium", rules  # the report, by the rule
    assert out["severity"] == "medium"
    assert out["signals"] == rules["signals"]
    # And it is LABELLED as the fixture it is. A keyless reply is an
    # ordinary successful completion; calling it `model` because the call
    # succeeded is precisely the fixture-as-judgement bug.
    assert out["severity_source"] == module.SOURCE_STUB


@pytest.mark.asyncio
async def test_a_model_answer_wins_and_the_output_says_so():
    module = _example_module()
    llm = _RecordingLLM(
        provider="openai",
        reply={
            "severity": "critical",
            "signals": ["auth outage"],
            "rationale": "every sign-in path is affected",
        },
    )

    out = await module.classify(REPORT, _config(_Caps(llm)))

    assert out["severity"] == "critical"
    assert out["signals"] == ["auth outage"]
    assert out["severity_source"] == module.SOURCE_MODEL


@pytest.mark.asyncio
async def test_a_model_may_revise_the_rule_DOWNWARD():
    """The rule is a fallback, not a floor, and that is the design.

    Codex round 10 read the manifest's "the answer is never worse than
    the rule" and pointed out the credentialled path does not provide
    that: `_valid` checks a reply's SHAPE, never its quality, so a model
    that answers replaces the rule outright.

    Two ways to reconcile a promise with the code, and only one is right
    here. Clamping the model to the rule's severity would make the
    example's clearest argument for calling a model **impossible to
    demonstrate**: `degraded-search` ships precisely because the keyword
    rule reads that report as `high` — it matches "errors" inside
    "nothing errors" — and the point is that a model corrects it DOWN to
    `medium`. A floor would pin it at `high` for ever.

    So the prose changed and the behaviour stayed, which means the
    behaviour now needs pinning: the rule says `medium` for this report,
    the model says `low`, and `low` is what comes out.
    """
    module = _example_module()
    assert module._by_the_rules(REPORT)["severity"] == "medium", (
        "the fixture's rule verdict moved; pick a model answer below it again"
    )
    order = module.CLASSIFY_SCHEMA["properties"]["severity"]["enum"]
    assert order.index("low") > order.index("medium"), (
        "the enum is ordered most severe first; this case needs a LOWER answer"
    )

    llm = _RecordingLLM(
        provider="openai",
        reply={
            "severity": "low",
            "signals": ["retries succeed"],
            "rationale": "intermittent and self-clearing",
        },
    )

    out = await module.classify(REPORT, _config(_Caps(llm)))

    assert out["severity"] == "low", (
        "the model's lower severity was clamped to the rule — the rule is a "
        "fallback, not a floor, and clamping breaks the degraded-search demo"
    )
    assert out["severity_source"] == module.SOURCE_MODEL


@pytest.mark.asyncio
async def test_an_unreachable_gateway_falls_back_to_the_rule_and_says_so():
    module = _example_module()
    llm = _RecordingLLM(raises=RuntimeError("gateway_unreachable"))

    out = await module.classify(REPORT, _config(_Caps(llm)))

    assert llm.calls, "the call was never attempted"
    assert out["severity"] == module._by_the_rules(REPORT)["severity"]
    assert out["severity_source"] == "rules"


@pytest.mark.asyncio
async def test_an_ungranted_llm_degrades_instead_of_raising():
    """The one the familiar spelling gets wrong.

    `getattr(caps, "llm", None)` does NOT return None for an ungranted
    member: the façade raises `CapabilityNotGranted`, a `RuntimeError`,
    and `getattr`'s default only swallows `AttributeError`. The example's
    kb node was written that way and could never have degraded — it would
    have killed the phase. So the node asks `caps.granted(...)`, and this
    drives a façade that raises exactly as the real one does.
    """
    module = _example_module()

    class _RaisingCaps:
        def __init__(self):
            self._grants = frozenset()

        def granted(self, name):
            return name in self._grants

        def __getattr__(self, name):
            if name in ("llm", "kb"):
                raise RuntimeError(f"capability {name!r} is not granted")
            raise AttributeError(name)

    out = await module.classify(REPORT, _config(_RaisingCaps()))

    assert out["severity"] == module._by_the_rules(REPORT)["severity"]
    assert out["severity_source"] == "rules"


@pytest.mark.asyncio
async def test_an_ungranted_kb_degrades_instead_of_raising():
    """The same predicate, on the node that had the bug.

    `gather_context` was written `kb = getattr(caps, "kb", None)` with a
    branch on `None` below it, and that branch could never run: the
    façade raises `CapabilityNotGranted`, which `getattr`'s default does
    not catch, and the call sat OUTSIDE the node's `try`. With the grant
    removed the node would have killed the phase while its own docstring
    promised it degraded. Fixing it without a test that fails on the old
    spelling would leave the next person free to write it back.
    """
    module = _example_module()

    class _RaisingCaps:
        def granted(self, name):
            return False

        def __getattr__(self, name):
            if name in ("llm", "kb"):
                raise RuntimeError(f"capability {name!r} is not granted")
            raise AttributeError(name)

    out = await module.gather_context({}, _config(_RaisingCaps()))

    assert out["context"] == ["no knowledge-base capability granted — skipped"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "node, caps",
    [
        ("classify", lambda cancelling: _Caps(cancelling)),
        ("gather_context", lambda cancelling: _CancellingKbCaps(cancelling)),
    ],
)
async def test_a_cancelled_phase_is_not_swallowed_as_a_failed_call(node, caps):
    """A deadline must survive both of the example's broad `except` clauses.

    The runner enforces `phases[].deadline_seconds` with
    `async with asyncio.timeout(...)`, which works by CANCELLING the
    task — so the cancellation is delivered at whatever the node is
    awaiting, which is the gateway call or the knowledge-base search.

    Measured rather than assumed, against a real socket and a real httpx
    client: cancellation reaches the node as `CancelledError`, httpx does
    not translate it into an `HTTPError`, and `except Exception` catches
    nothing, so the phase fails with its deadline named.

    That is CPython's guarantee, not ours, and a test of it would be a
    test of CPython. The property pinned HERE is the one that is ours and
    that an edit could take away: **neither clause may widen to
    `BaseException`.** If one did, every deadline would become a silent
    fall back to the keyword rule and the run would report `complete`
    after its budget had run out — the failure mode a reader of the
    docstring is entitled to assume cannot happen.
    """
    module = _example_module()

    class _Cancelling:
        """The task being cancelled where it awaits."""

        async def complete(self, step, messages, **kwargs):
            raise asyncio.CancelledError()

        def available(self):
            return True

        async def search(self, **kwargs):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await getattr(module, node)(REPORT, _config(caps(_Cancelling())))


@pytest.mark.asyncio
async def test_outside_a_run_there_is_no_facade_and_the_rule_answers():
    """A notebook, or a unit test of the graph alone: no config at all."""
    module = _example_module()
    out = await module.classify(REPORT, None)
    assert out["severity_source"] == "rules"


@pytest.mark.asyncio
async def test_the_draft_step_is_wired_the_same_way():
    module = _example_module()
    llm = _RecordingLLM(
        provider="openai", reply={"next_steps": ["page the auth on-call"]}
    )
    state = {
        "prior_analysis": {
            "severity": "high",
            "service": "auth-api",
            "summary": "auth-api: Login is slow — triaged high",
            "severity_source": "model",
        },
        "context": ["kb-hit"],
    }

    out = await module.draft(state, _config(_Caps(llm)))

    (step, _messages, kwargs) = llm.calls[0]
    assert step == "draft"
    assert kwargs["response_format"]["json_schema"]["name"] == "draft"
    assert json.loads(kwargs["librerun"]["stub_reply"]) == {
        "next_steps": module._checklist("high", "auth-api")
    }
    assert out["structured"]["next_steps"] == ["page the auth on-call"]
    assert out["structured"]["next_steps_source"] == "model"
    # The two provenances are separate facts: this phase reached a model,
    # the previous one did too, and either could have failed alone.
    assert out["structured"]["severity_source"] == "model"


@pytest.mark.asyncio
async def test_a_refused_draft_keeps_the_checklist():
    module = _example_module()
    llm = _RecordingLLM(raises=RuntimeError("gateway_timeout"))
    state = {"prior_analysis": {"severity": "low", "service": "auth-api"}}

    out = await module.draft(state, _config(_Caps(llm)))

    assert out["structured"]["next_steps"] == module._checklist("low", "auth-api")
    assert out["structured"]["next_steps_source"] == "rules"


def test_both_scenarios_load_and_disagree_about_severity():
    """Two scenarios, and they must not triage the same.

    The batch asks for a second scenario; a second one that lands on the
    same severity would add a row to the demo and prove nothing. These
    differ by the RULE, which is what a keyless demo shows — so a keyless
    gallery displays two different answers rather than one enum member
    twice.
    """
    module = _example_module()
    directory = EXAMPLE_DIR / _manifest()["scenarios"]
    files = sorted(directory.glob("*.json"))
    assert len(files) >= 2, [f.name for f in files]

    verdicts = {}
    for path in files:
        scenario = json.loads(path.read_text())
        assert scenario["name"] and scenario["description"]
        verdicts[path.stem] = module._by_the_rules(
            {"user_inputs": scenario["user_inputs"]}
        )["severity"]

    assert len(set(verdicts.values())) > 1, verdicts
    # And none of them is the value the stub would invent unprompted, for
    # at least one scenario — otherwise the fixture could go missing
    # without the demo changing.
    first_enum_member = module.CLASSIFY_SCHEMA["properties"]["severity"]["enum"][0]
    assert any(v != first_enum_member for v in verdicts.values()), verdicts


@pytest.mark.asyncio
async def test_a_reply_with_no_provider_is_unattributed_not_a_model():
    """The safe default when the gateway does not say who answered.

    Defaulting to `model` is how a gateway that stopped sending its
    `librerun` envelope would silently reintroduce the bug this label
    exists to prevent — the content would still parse, the call would
    still have succeeded, and a fixture would be shown as a judgement
    again. `unattributed` cannot be wrong by omission.
    """
    module = _example_module()
    llm = _RecordingLLM(envelope=False)

    out = await module.classify(REPORT, _config(_Caps(llm)))

    assert llm.calls, "the call was never attempted"
    assert out["severity_source"] == module.SOURCE_UNKNOWN
    assert out["severity_source"] != module.SOURCE_MODEL


@pytest.mark.asyncio
async def test_the_draft_step_labels_its_fixture_too():
    """The same question on the other node — one fixed node is a fix, two
    is a rule."""
    module = _example_module()
    state = {"prior_analysis": {"severity": "high", "service": "auth-api"}}

    out = await module.draft(state, _config(_Caps(_RecordingLLM())))

    # Keyless, the checklist comes back as this agent's own fixture...
    assert out["structured"]["next_steps"] == module._checklist("high", "auth-api")
    # ...and says so, rather than claiming a model wrote it.
    assert out["structured"]["next_steps_source"] == module.SOURCE_STUB


def test_every_source_value_the_nodes_can_emit_is_one_of_the_four():
    """The vocabulary is closed, and `_source` is the only thing that
    decides it — a node assigning a literal would escape the rule."""
    module = _example_module()
    known = {
        module.SOURCE_RULES,
        module.SOURCE_STUB,
        module.SOURCE_MODEL,
        module.SOURCE_UNKNOWN,
    }
    assert len(known) == 4, known
    produced = {
        module._source(None, used=False),
        module._source("stub", used=True),
        module._source("STUB", used=True),   # the admin's word, any case
        module._source("openai", used=True),
        module._source(None, used=True),
    }
    assert produced == known, produced

    _no_node_decides_the_label_by_hand()


SOURCE_FIELDS = ("severity_source", "next_steps_source")


def _no_node_decides_the_label_by_hand():
    """Every `*_source` value is `_source(...)` or a carry-forward.

    Stated as a WHITELIST of two accepted shapes rather than a blacklist
    of literals, because the blacklist version did not catch the defect
    it was written for. The pre-fix code read

        source = "model"
        ...
        return {"severity_source": source}

    and the value there is a `Name`, not a `Constant` — so the rule
    inspected the one spelling the bug did not use, and would have
    passed the original implementation unchanged (Codex P2). Any shape
    that is not one of the two below fails, including a name, a
    conditional expression and an `or` chain.
    """
    tree = ast.parse((EXAMPLE_DIR / "agent.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value in SOURCE_FIELDS):
                continue
            # (1) the deciding call: `_source(provider, used=...)`.
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "_source"
            ):
                continue
            # (2) carrying a verdict forward — and ONLY that. Three
            # parts are load-bearing:
            #   the receiver    `prior` (the previous phase) or `state`
            #                   (this graph's own channel). Those are the
            #                   two places a verdict can already exist;
            #   the SAME field  `prior.get("next_steps_source")` under
            #                   `severity_source` labels one with the
            #                   other;
            #   SOURCE_RULES    any other default DECIDES the label when
            #                   the field is absent, which is the whole
            #                   defect. `prior.get("severity_source",
            #                   SOURCE_MODEL)` calls a rule-derived
            #                   result model-derived, and the first
            #                   version of this rule allowed it — an
            #                   exception wide enough to re-admit the bug
            #                   it was carved out of (Codex P2).
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id in ("prior", "state")
                and len(value.args) == 2
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == key.value
                and isinstance(value.args[1], ast.Name)
                and value.args[1].id == "SOURCE_RULES"
            ):
                continue
            raise AssertionError(
                f"line {value.lineno}: {key.value} is set from "
                f"{ast.dump(value)[:60]}… — it must be `_source(...)`, or a "
                f"`.get(<field>, SOURCE_*)` carrying a previous phase's verdict. "
                f"Deciding the label anywhere else is how a fixture gets shown "
                f"as a judgement."
            )


@pytest.mark.parametrize(
    "reply, why",
    [
        ({"severity": "high", "signals": 5, "rationale": "x"}, "signals is an int"),
        ({"severity": "high", "signals": "down", "rationale": "x"}, "signals is a str"),
        ({"severity": "high", "signals": [1, 2], "rationale": "x"}, "signals holds ints"),
        ({"severity": "high", "signals": [], "rationale": 7}, "rationale is a number"),
        ({"severity": "nuclear", "signals": [], "rationale": "x"}, "severity off-enum"),
        ({"signals": [], "rationale": "x"}, "severity missing"),
        ([1, 2, 3], "not an object at all"),
    ],
)
@pytest.mark.asyncio
async def test_a_schema_invalid_reply_falls_back_instead_of_raising(reply, why):
    """`response_format` is a request, not a guarantee.

    The gateway forwards it; it cannot make a provider honour it, and
    D13 exists so an admin can retarget a step to a provider that
    honours it loosely or not at all. Checking one field is not checking
    the answer: a valid `severity` beside an integer `signals` passed the
    old enum test and then raised `TypeError` in the comprehension that
    consumed it — killing the phase, which is the exact opposite of the
    deterministic fallback this node promises.
    """
    module = _example_module()
    llm = _RecordingLLM(provider="openai", reply=reply)

    out = await module.classify(REPORT, _config(_Caps(llm)))

    assert llm.calls, "the call was never attempted"
    assert out["severity"] == module._by_the_rules(REPORT)["severity"], why
    assert out["severity_source"] == module.SOURCE_RULES, why


@pytest.mark.asyncio
async def test_a_schema_invalid_draft_keeps_the_checklist():
    module = _example_module()
    state = {"prior_analysis": {"severity": "low", "service": "auth-api"}}

    for reply in ({"next_steps": "page someone"}, {"next_steps": []}, {"next_steps": [3]}):
        llm = _RecordingLLM(provider="openai", reply=reply)
        out = await module.draft(state, _config(_Caps(llm)))
        assert out["structured"]["next_steps"] == module._checklist("low", "auth-api"), reply
        assert out["structured"]["next_steps_source"] == module.SOURCE_RULES, reply


def test_the_validator_really_enforces_the_schema():
    """`_valid` is the chassis's own `Draft202012Validator`.

    Not a hand-rolled checker: `intake.py` has validated agent input
    that way since B8 and `jsonschema` is a pinned direct dependency, so
    an in-process agent has it for free. The forty lines this replaced
    took three review rounds to stop missing keywords. These assertions
    stay because they say what validation has to MEAN here, whoever
    implements it.
    """
    module = _example_module()
    schema = {
        # `required` applies to objects, so a schema without this admits
        # `None` — which is why the real schemas declare it too.
        "type": "object",
        "required": ["a", "b"],
        "properties": {
            "a": {"type": "string", "enum": ["x", "y"]},
            "b": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        },
    }
    assert module._valid({"a": "x", "b": ["ok"]}, schema)
    assert not module._valid({"a": "z", "b": ["ok"]}, schema)       # off-enum
    assert not module._valid({"a": "x", "b": []}, schema)           # minItems
    assert not module._valid({"a": "x", "b": "ok"}, schema)         # str, not list
    assert not module._valid({"a": "x", "b": [1]}, schema)          # wrong item type
    assert not module._valid({"a": "x"}, schema)                    # missing required
    assert not module._valid(None, schema)


def test_every_keyword_the_schemas_declare_is_one_jsonschema_knows():
    """A misspelled keyword is a bound that silently does not exist.

    Moving to a real validator removed the old failure — a keyword the
    implementation ignored — but NOT this one, which is the same defect
    one level out. Measured: `{"type": "array", "maxItmes": 2}` accepts
    a three-element list, and `check_schema` passes it, because unknown
    keywords are legal JSON Schema. So `maxItmes` looks like a bound,
    reads like a bound in review, and enforces nothing.

    The vocabulary comes from the library rather than from a list of my
    own, so it cannot go stale the way `VALIDATED_KEYWORDS` would have.
    """
    module = _example_module()
    from jsonschema import Draft202012Validator

    known = set(Draft202012Validator.VALIDATORS) | {"$schema", "$id", "title", "description"}

    def keywords(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "properties" and isinstance(value, dict):
                    for spec in value.values():
                        yield from keywords(spec)
                    yield key
                    continue
                yield key
                yield from keywords(value)

    for schema in (module.CLASSIFY_SCHEMA, module.DRAFT_SCHEMA):
        unknown = set(keywords(schema)) - known
        assert not unknown, (
            f"{sorted(unknown)} are not JSON Schema keywords, so the "
            f"validator ignores them and whatever bound they look like is "
            f"not enforced. A typo here is invisible in review and silent "
            f"at runtime."
        )


@pytest.mark.parametrize(
    "reply, why",
    [
        ({"severity": "high", "signals": ["x"] * 7, "rationale": "ok"}, "signals over maxItems"),
        ({"severity": "high", "signals": ["y" * 200], "rationale": "ok"}, "signal over maxLength"),
        ({"severity": "high", "signals": [], "rationale": "z" * 400}, "rationale over maxLength"),
    ],
)
@pytest.mark.asyncio
async def test_a_reply_past_the_schemas_upper_bounds_falls_back(reply, why):
    module = _example_module()
    llm = _RecordingLLM(provider="openai", reply=reply)

    out = await module.classify(REPORT, _config(_Caps(llm)))

    assert out["severity"] == module._by_the_rules(REPORT)["severity"], why
    assert out["severity_source"] == module.SOURCE_RULES, why


@pytest.mark.asyncio
async def test_a_draft_past_maxitems_falls_back():
    module = _example_module()
    state = {"prior_analysis": {"severity": "low", "service": "auth-api"}}
    llm = _RecordingLLM(provider="openai", reply={"next_steps": [f"s{i}" for i in range(7)]})

    out = await module.draft(state, _config(_Caps(llm)))

    assert out["structured"]["next_steps"] == module._checklist("low", "auth-api")
    assert out["structured"]["next_steps_source"] == module.SOURCE_RULES


@pytest.mark.asyncio
async def test_a_reply_with_an_undeclared_field_falls_back():
    """`additionalProperties: false` is a bound like any other.

    Both schemas declare it, and the hand-rolled validator never looked
    at keys outside `properties` — so a provider adding `confidence` to
    its answer was accepted (Codex P2). Worse, the coverage guard listed
    `additionalProperties` as covered, with a comment claiming it was
    "enforced by construction". That was a rationalisation written to
    make the list complete, not a fact: **the guard certified a keyword
    nothing enforced.**

    The move to `Draft202012Validator` fixes it as a side effect, which
    is exactly why this test exists — a property nothing pins is one a
    later change can quietly drop again.
    """
    module = _example_module()
    assert module.CLASSIFY_SCHEMA["additionalProperties"] is False
    llm = _RecordingLLM(
        provider="openai",
        reply={"severity": "high", "signals": [], "rationale": "ok", "confidence": 0.9},
    )

    out = await module.classify(REPORT, _config(_Caps(llm)))

    assert out["severity"] == module._by_the_rules(REPORT)["severity"]
    assert out["severity_source"] == module.SOURCE_RULES

