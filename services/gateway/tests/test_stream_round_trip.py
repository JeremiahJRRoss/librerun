"""The streamed path is a second path, and it has been wrong five times.

Decision 50 (a streamed tool call recorded as an empty choice), decision
53 (the legacy `function_call` spelling), decision 86 (`n` — one choice
in the stream however many were asked for), decision 88 (`logprobs`
filled by `complete()` and dropped by `stream()`), and the embeddings
reply in decision 55. Every one was the same shape: the non-streaming
path was written or fixed, and the streamed one was not.

Five instances is enough to stop finding them one at a time. This drives
every reply shape the stub can produce through
`complete()` -> `stream()` -> `main._StreamAssembly` and requires the
reassembled choices to match the direct ones. A future field added to a
message and not to the delta fails here rather than in a review.

The assembly is what the gateway's OWN view of a streamed reply is built
from — the span's walked content, and the cost — so a field it cannot
reassemble is a field the trace loses for streaming callers only, which
is precisely the asymmetry that keeps being shipped.
"""
from __future__ import annotations

import pytest

from gateway import stub_provider
from gateway.main import _StreamAssembly

# What a caller can ask the stub for, by the shape it comes back as.
SHAPES = {
    "plain text": {"messages": [{"role": "user", "content": "hi"}]},
    "several choices": {"messages": [{"role": "user", "content": "hi"}], "n": 3},
    "a forced tool call": {
        "messages": [],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string"}},
                        "required": ["q"],
                    },
                },
            }
        ],
        "tool_choice": "required",
    },
    "a legacy function call": {
        "messages": [],
        "functions": [
            {
                "name": "lookup",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                    "required": ["q"],
                },
            }
        ],
        "function_call": {"name": "lookup"},
    },
    "structured output": {
        "messages": [],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "x",
                "schema": {
                    "type": "object",
                    "properties": {"a": {"type": "integer"}},
                    "required": ["a"],
                },
            },
        },
    },
    "a scripted reply": {
        "messages": [],
        "librerun": {"stub_reply": "a canned answer"},
    },
}

# `logprobs` is deliberately not reassembled, and this says so rather
# than leaving a silent hole. The client receives it on the raw chunk —
# the gateway forwards chunks verbatim — and no span records it on
# EITHER path, since `walked_choices` keeps content, refusals, tool
# calls and audio counts and never per-token probabilities. So the
# assembly has nothing to do with it. If telemetry ever starts recording
# logprobs, delete this line and the test will say what else to change.
NOT_REASSEMBLED = {"logprobs"}


def _round_trip(body: dict):
    direct = stub_provider.complete(body, model="stub", scenario=None)
    assembly = _StreamAssembly()
    for chunk in stub_provider.stream(direct):
        assembly.add(chunk)
    return direct, assembly.choices()


@pytest.mark.parametrize("label", sorted(SHAPES))
def test_a_streamed_reply_reassembles_into_the_direct_one(label):
    direct, rebuilt = _round_trip(SHAPES[label])
    assert len(rebuilt) == len(direct["choices"]), label

    for sent, back in zip(direct["choices"], rebuilt):
        assert back["index"] == sent["index"]
        assert back["finish_reason"] == sent["finish_reason"], (
            "the stop reason is what a caller switches on, and a streamed "
            "reply that ends differently from the direct one is two contracts"
        )
        expected, actual = sent["message"], back["message"]
        lost = [
            key
            for key, value in expected.items()
            if value is not None and key not in actual
        ]
        assert not lost, f"{label}: message fields lost in the stream: {lost}"
        for key, value in expected.items():
            if value is None or key in NOT_REASSEMBLED:
                continue
            assert actual.get(key) == value, f"{label}: message.{key} differs"


@pytest.mark.parametrize("label", sorted(SHAPES))
def test_every_choice_reaches_the_stream(label):
    """The `n` failure on its own, stated as a property: one chunk set
    per choice, each under its own index."""
    direct, _ = _round_trip(SHAPES[label])
    indexes = sorted(
        choice["index"]
        for chunk in stub_provider.stream(direct)
        for choice in chunk["choices"]
    )
    assert set(indexes) == {c["index"] for c in direct["choices"]}


def test_the_excluded_field_is_still_delivered_to_the_caller():
    """`logprobs` is excluded above because the ASSEMBLY has no use for
    it — not because it may go missing. The client reads the chunks the
    gateway forwards, so it must be there."""
    direct, _ = _round_trip(
        {"messages": [{"role": "user", "content": "hi"}], "logprobs": True}
    )
    carried = [
        choice
        for chunk in stub_provider.stream(direct)
        for choice in chunk["choices"]
        if choice.get("logprobs") is not None
    ]
    assert carried, "logprobs never reached the wire"
