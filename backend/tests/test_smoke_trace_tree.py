"""``librerun_smoke.assert_one_tree`` (blueprint S4 Accept).

The smoke's trace assertion used to be "the run has a trace id", which
a run always has — the id is minted when the span starts, so it says
nothing about export and nothing at all about shape. Before S4 a gated
run was TWO traces joined by a link: the id existed, the run page linked
to one of them, and the viewer showed half the story. That is invisible
from the API, which is why the assertion moved to the viewer's own
answer.

The assertion is the part that has to be right, so it is tested here
against fabricated Jaeger responses rather than only exercised in CI
against a live one: a checker that accepts everything is the failure
mode, and a clean tree cannot reveal it.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parents[2] / "scripts" / "librerun_smoke.py"


def _smoke():
    spec = importlib.util.spec_from_file_location("librerun_smoke_under_test", SMOKE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _llm_span(span_id, parent, trace="abc123", **overrides):
    """A gateway LLM span, with what blueprint S4a requires of one."""
    tags = {
        "gen_ai.request.model": "gpt-4o",
        "gen_ai.usage.input_tokens": 120,
        "gen_ai.usage.output_tokens": 40,
        "librerun.cost_usd": 0.0007,
    }
    tags.update(overrides)
    span = _span(span_id, "chat gpt-4o", parent=parent, trace=trace)
    span["tags"] = [{"key": k, "value": v} for k, v in tags.items()]
    return span


def _span(span_id, name, parent=None, trace="abc123"):
    span = {"spanID": span_id, "operationName": name, "traceID": trace, "references": []}
    if parent:
        span["references"] = [{"refType": "CHILD_OF", "spanID": parent, "traceID": trace}]
    return span


@pytest.fixture
def smoke(monkeypatch):
    module = _smoke()
    monkeypatch.setattr(module.time, "sleep", lambda _s: None)
    return module


def _answer(smoke, monkeypatch, spans):
    def _request(method, url, **kwargs):  # noqa: ARG001
        return 200, {"data": [{"spans": spans}]} if spans else {"data": []}

    monkeypatch.setattr(smoke, "_request", _request)


def test_one_root_with_every_phase_under_it_passes(smoke, monkeypatch):
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _span("2", "phase analyze", parent="1"),
        _span("3", "phase investigate", parent="1"),
        _llm_span("4", parent="3"),
    ])
    result = smoke.assert_one_tree("http://viewer", "abc123", attempts=1)
    assert result["root"] == "run" and result["spans"] == 4
    assert result["llm_spans"]["count"] == 1
    assert result["llm_spans"]["spans"][0]["model"] == "gpt-4o"


def _answers(smoke, monkeypatch, pages):
    """Serve a different trace on each fetch, the last one repeating."""
    seen = {"n": 0}

    def _request(method, url, **kwargs):  # noqa: ARG001
        spans = pages[min(seen["n"], len(pages) - 1)]
        seen["n"] += 1
        return 200, {"data": [{"spans": spans}]} if spans else {"data": []}

    monkeypatch.setattr(smoke, "_request", _request)
    return seen


def test_a_half_assembled_trace_is_waited_for_not_reported(smoke, monkeypatch):
    """Two processes export into this trace on their own batch
    schedules. Read too early, an LLM span whose parent has not landed
    yet looks exactly like a second root — the pre-S4 defect's
    signature, on a run that never had it. That is what turned this gate
    red on a correct tree.
    """
    partial = [
        _span("1", "run"),
        _llm_span("4", parent="3"),  # its phase span has not arrived
    ]
    complete = [
        _span("1", "run"),
        _span("3", "phase investigate", parent="1"),
        _llm_span("4", parent="3"),
    ]
    seen = _answers(smoke, monkeypatch, [partial, complete])

    result = smoke.assert_one_tree("http://viewer", "abc123", attempts=5, delay=0)

    assert result["root"] == "run" and result["spans"] == 3
    assert seen["n"] >= 2, "the gate accepted the first, incomplete read"


def test_a_lone_llm_span_is_waited_for_not_reported_as_the_root(smoke, monkeypatch):
    """The same race from the other side. Read before any of the
    backend's spans has landed, the trace can be the gateway's first LLM
    span alone: one root, and an LLM span present, so a wait for "one
    root and an LLM span" was over — and the gate reported "the root
    span is 'chat gpt-4o', not 'run'" on a correct run (main's push run
    on 74930ca). The root's name is part of the precondition too.
    """
    early = [_llm_span("4", parent="3")]  # nothing of the backend's yet
    complete = [
        _span("1", "run"),
        _span("3", "phase investigate", parent="1"),
        _llm_span("4", parent="3"),
    ]
    seen = _answers(smoke, monkeypatch, [early, complete])

    result = smoke.assert_one_tree("http://viewer", "abc123", attempts=5, delay=0)

    assert result["root"] == "run" and result["spans"] == 3
    assert seen["n"] >= 2, "the gate accepted a lone LLM span as the tree"


def test_a_root_that_never_becomes_run_still_fails(smoke, monkeypatch):
    """…and waiting for it changes when the gate looks, never what it
    accepts: a tree whose one root is not the submission span is still
    that tree after the last attempt, and fails with its own message."""
    _answer(smoke, monkeypatch, [
        _span("1", "phase analyze"),
        _llm_span("4", parent="1"),
    ])

    with pytest.raises(smoke.SmokeFailure, match="not 'run'"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=3, delay=0)


def test_a_trace_that_never_settles_still_fails(smoke, monkeypatch):
    """Waiting changes when the gate looks, never what it accepts: a run
    that is genuinely more than one tree is still more than one tree
    after the last attempt."""
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _span("2", "phase analyze"),
        _llm_span("4", parent="2"),
    ])

    with pytest.raises(smoke.SmokeFailure, match="trees, not one"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=3, delay=0)


def test_waiting_does_not_invent_an_llm_span(smoke, monkeypatch):
    """The other half of the wait condition, for the same reason."""
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _span("2", "phase analyze", parent="1"),
    ])

    with pytest.raises(smoke.SmokeFailure, match="no LLM span"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=3, delay=0)


def test_a_tree_with_no_llm_span_fails(smoke, monkeypatch):
    """Blueprint S4a: every model call goes through the gateway, which
    writes exactly one span per call. A completed run whose trace has
    none called a model some other way — which is the one thing the
    gateway exists to make impossible."""
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _span("2", "phase analyze", parent="1"),
    ])
    with pytest.raises(smoke.SmokeFailure, match="no LLM span"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)
    # …and a caller asserting only the SHAPE of a tree says so.
    assert smoke.assert_one_tree(
        "http://viewer", "abc123", attempts=1, require_llm=False
    )["root"] == "run"


@pytest.mark.parametrize(
    "missing",
    ["gen_ai.request.model", "gen_ai.usage.input_tokens", "librerun.cost_usd"],
)
def test_an_llm_span_missing_model_tokens_or_cost_fails(smoke, monkeypatch, missing):
    """Promise 3 is model, tokens AND cost on every call. A span carrying
    two of the three is the shape a half-wired gateway produces."""
    span = _llm_span("4", parent="1")
    span["tags"] = [t for t in span["tags"] if t["key"] != missing]
    _answer(smoke, monkeypatch, [_span("1", "run"), span])

    with pytest.raises(smoke.SmokeFailure, match="missing"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_a_zero_cost_llm_span_fails(smoke, monkeypatch):
    """Keyless runs are costed against the model the stub stands in for,
    precisely so this assertion is not one that only holds where somebody
    has a provider account. A zero is a gateway that stopped pricing."""
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _llm_span("4", parent="1", **{"librerun.cost_usd": 0.0}),
    ])

    with pytest.raises(smoke.SmokeFailure, match="cost of"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_a_redacted_model_name_fails(smoke, monkeypatch):
    """"Non-empty" is not "a model".

    The export walks every string it was not told the chassis wrote, and
    the recognizers read a dated model name as a person's — so an
    unstamped `gen_ai.request.model` reaches the viewer as a placeholder,
    and the run page cannot say which model answered. A gate that
    accepted it would be reporting success by not looking.
    """
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _llm_span("4", parent="1", **{"gen_ai.request.model": "[REDACTED_PERSON_1]"}),
    ])

    with pytest.raises(smoke.SmokeFailure, match="redacted what the chassis resolved"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_a_redacted_span_name_fails_too(smoke, monkeypatch):
    """The GenAI convention makes the name `{operation} {model}`, so a
    redacted name is a redacted model on the field the viewer shows
    first — and the model attribute beside it can look perfectly fine."""
    span = _llm_span("4", parent="1")
    span["operationName"] = "chat [REDACTED_PERSON_1]"
    _answer(smoke, monkeypatch, [_span("1", "run"), span])

    with pytest.raises(smoke.SmokeFailure, match="redacted what the chassis resolved"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_a_redacted_response_model_fails_too(smoke, monkeypatch):
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _llm_span(
            "4",
            parent="1",
            **{"gen_ai.response.model": "[REDACTED_PERSON_1]-20250514"},
        ),
    ])

    with pytest.raises(smoke.SmokeFailure, match="redacted what the chassis resolved"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_two_roots_is_the_defect_s4_closed(smoke, monkeypatch):
    """The pre-S4 shape exactly: each phase its own trace root, joined by
    a link the viewer does not draw as parenthood."""
    _answer(smoke, monkeypatch, [
        _span("1", "phase analyze"),
        _span("2", "phase investigate"),
    ])
    with pytest.raises(smoke.SmokeFailure, match="2 trees, not one"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_a_root_that_is_not_the_submission_span_fails(smoke, monkeypatch):
    _answer(smoke, monkeypatch, [
        _span("1", "phase analyze"),
        _span("2", "llm call", parent="1"),
    ])
    with pytest.raises(smoke.SmokeFailure, match="not 'run'"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_spans_from_more_than_one_trace_fail(smoke, monkeypatch):
    _answer(smoke, monkeypatch, [
        _span("1", "run"),
        _span("2", "phase analyze", parent="1"),
        _span("3", "phase investigate", parent="1", trace="deadbeef"),
    ])
    with pytest.raises(smoke.SmokeFailure, match="more than one trace id"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)


def test_an_empty_viewer_fails_rather_than_passing_quietly(smoke, monkeypatch):
    """The one that matters most: a viewer with nothing in it must not
    read as a clean tree. An assertion that treats 'no spans' as 'no
    problems' is how a broken exporter ships."""
    _answer(smoke, monkeypatch, [])
    with pytest.raises(smoke.SmokeFailure, match="no spans"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=2, delay=0)


def test_a_parent_outside_the_returned_set_still_counts_as_a_root(smoke, monkeypatch):
    """A reference to a span the viewer did not return is not parenthood
    it can draw, so such a span is a root — two of them are two trees,
    which is the case this must not miss."""
    _answer(smoke, monkeypatch, [
        _span("2", "phase analyze", parent="missing"),
        _span("3", "phase investigate", parent="alsomissing"),
    ])
    with pytest.raises(smoke.SmokeFailure, match="2 trees, not one"):
        smoke.assert_one_tree("http://viewer", "abc123", attempts=1)
