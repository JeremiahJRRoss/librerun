"""`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` has four values,
and this module had two behaviours (blueprint S4a; Codex round 10, P2).

`capture_content()` is a bool — anything but `NO_CONTENT` wrote the
payload to the span attribute AND the event. So an operator who chose
`SPAN_ONLY` or `EVENT_ONLY` — the two settings whose entire purpose is
to pick a destination — still got content through the one they had
explicitly turned off, defeating whatever routing or retention policy
made them choose it.

Resolving the enum carefully and then collapsing it to a bool is a
setting that does not exist, which is decision 54 one field over.
"""
from __future__ import annotations

import pytest

from gateway import telemetry

MESSAGES = [{"role": "user", "content": "hello"}]
RESPONSE = {
    "choices": [
        {"index": 0, "finish_reason": "stop", "message": {"content": "hi"}}
    ],
    "usage": {},
}


class _Span:
    """Records which destination was written, and nothing else."""

    def __init__(self):
        self.attributes: dict = {}
        self.events: list = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def add_event(self, name, attributes=None):
        self.events.append((name, attributes or {}))

    def get_span_context(self):
        class _Ctx:
            trace_id = 1
            span_id = 2

        return _Ctx()


class _Step:
    step_id = "think"
    provider = "openai"
    model = "gpt-4o"


@pytest.fixture
def capture(monkeypatch):
    def _set(value):
        monkeypatch.setenv(telemetry._CONTENT_VAR, value)

    return _set


# (value, on the span?, in an event?)
CASES = [
    ("SPAN_AND_EVENT", True, True),
    ("SPAN_ONLY", True, False),
    ("EVENT_ONLY", False, True),
    ("NO_CONTENT", False, False),
]


def test_every_enum_value_is_covered():
    """If a fifth value is added, this scan must be extended with it
    rather than silently testing three quarters of the setting."""
    assert {value for value, _, _ in CASES} == set(telemetry._VALID_CONTENT_VALUES)


@pytest.mark.parametrize("value,on_span,in_event", CASES)
def test_the_prompt_goes_only_where_it_was_asked(capture, value, on_span, in_event):
    capture(value)
    span = _Span()

    telemetry.record_prompt(span, MESSAGES)

    assert ("gen_ai.input.messages" in span.attributes) is on_span, (
        f"{value}: span attribute presence is wrong"
    )
    assert bool(span.events) is in_event, f"{value}: event presence is wrong"


@pytest.mark.parametrize("value,on_span,in_event", CASES)
def test_the_response_goes_only_where_it_was_asked(capture, value, on_span, in_event):
    capture(value)
    span = _Span()

    telemetry.record_response(span, RESPONSE, _Step(), None, operation="chat")

    assert ("gen_ai.output.messages" in span.attributes) is on_span, (
        f"{value}: span attribute presence is wrong"
    )
    content_events = [
        e for e in span.events if "gen_ai.output.messages" in (e[1] or {})
    ]
    assert bool(content_events) is in_event, f"{value}: event presence is wrong"


def test_an_unrecognised_value_still_fails_closed(capture):
    """The existing rule, unchanged by the split: the only person who
    writes this variable is one trying to suppress content."""
    capture("NO_CONTNET")
    span = _Span()

    telemetry.record_prompt(span, MESSAGES)
    telemetry.record_response(span, RESPONSE, _Step(), None, operation="chat")

    assert span.attributes.get("gen_ai.input.messages") is None
    assert span.attributes.get("gen_ai.output.messages") is None
    assert not span.events
