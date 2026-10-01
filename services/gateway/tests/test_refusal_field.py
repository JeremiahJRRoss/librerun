"""`refusal` is prose, everywhere it appears (blueprint S4a; Codex round
9, one P1 and one P2).

OpenAI puts a safety refusal in `message.refusal` and commonly leaves
`content` null. The gateway treated that field as structure on the way
out and ignored it on the way back, so the same string was mishandled
twice in opposite directions:

- **Outbound**, it fell to the `structure` walker, which checks a string
  without rewriting it and whose identifier check deliberately omits
  person-name NER. A refusal naming a customer went to the provider
  VERBATIM while the identical name in `content` beside it became
  `[REDACTED_PERSON_1]`. And a refusal carrying an email did the
  opposite — tripped the identifier check and refused the WHOLE request,
  because a string the walk may not rewrite can only be refused. So
  replaying an ordinary assistant turn was impossible.
- **Inbound**, the completion walker read only `content` and
  `tool_calls`, so a refusal reply was recorded as a choice with nothing
  in it. The fourth spelling of that failure (decisions 50, 53, 55) and
  the one that matters most to read back: a refusal is exactly the reply
  an operator opens the trace to understand.
"""
from __future__ import annotations

import json

import pytest

from gateway import redaction, telemetry

TENANT = "11111111-1111-4111-8111-111111111111"
NAME = "Margaret Hollingsworth"
EMAIL = "margaret@acme.com"


def _walk(messages):
    return redaction.redact_request(
        {"model": "librerun/think", "messages": messages},
        tenant_id=TENANT,
        enabled=True,
    )


def test_a_refusal_is_redacted_exactly_like_the_content_beside_it():
    """The pairing is the point: one request, one name, two positions.
    Any difference between them is the bug."""
    out, report = _walk(
        [
            {"role": "user", "content": f"{NAME} ({EMAIL}) opened the ticket."},
            {"role": "assistant", "refusal": f"I can't discuss {NAME} at {EMAIL}."},
        ]
    )
    blob = json.dumps(out)

    assert NAME not in blob, "the person's name left the gateway"
    assert EMAIL not in blob, "the email left the gateway"
    assert "$.messages[1].refusal" in report.redacted_paths


def test_a_refusal_carrying_an_identifier_is_rewritten_not_refused():
    """Treated as structure, an email here refused the entire request —
    so a legal assistant turn could not be replayed at all."""
    out, _ = _walk([{"role": "assistant", "refusal": f"Cannot reach {EMAIL}."}])

    assert EMAIL not in json.dumps(out)
    assert out["messages"][0]["refusal"]


def test_redaction_off_still_leaves_the_refusal_intact():
    """The switch governs what the model sees; it must govern this field
    the same way it governs `content`, not more and not less."""
    out, _ = redaction.redact_request(
        {
            "model": "librerun/think",
            "messages": [{"role": "assistant", "refusal": f"About {NAME}."}],
        },
        tenant_id=TENANT,
        enabled=False,
    )
    assert out["messages"][0]["refusal"] == f"About {NAME}."


def test_the_span_records_a_refusal_reply():
    """`content` null and `refusal` set is a complete answer, not an
    empty one."""
    walked = telemetry.walked_choices(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "refusal": "I can't help with that.",
                    },
                }
            ]
        }
    )
    assert walked[0].get("refusal"), "the span recorded a refusal reply as nothing"


def test_the_span_records_a_replayed_refusal_on_the_prompt_side():
    """An assistant turn replayed as input carries the same field, and
    it is model-visible text like any other."""
    walked = telemetry.walked_messages(
        [{"role": "assistant", "refusal": "I can't help with that."}]
    )
    assert walked[0].get("refusal")


@pytest.mark.parametrize("walker", ["walked_choices", "walked_messages"])
def test_the_recorded_refusal_is_walked(walker):
    """It is text the model produced or read, so it goes through the
    redactor before the span keeps it — like every other position."""
    payload = (
        {"choices": [{"index": 0, "message": {"content": None, "refusal": f"About {NAME}."}}]}
        if walker == "walked_choices"
        else [{"role": "assistant", "refusal": f"About {NAME}."}]
    )
    walked = getattr(telemetry, walker)(payload)
    assert NAME not in json.dumps(walked), "the span kept an unwalked name"


def test_a_streamed_refusal_is_assembled_for_the_span():
    """A refusal streams in `delta.refusal` pieces like content does.
    Decision 53's rule: a field recorded on one side is recorded on
    both, or the streamed path quietly says something different from the
    non-streamed one for the same reply."""
    from gateway.main import _StreamAssembly

    assembly = _StreamAssembly()
    for piece in ("I can't ", "help with ", "that."):
        assembly.add({"choices": [{"index": 0, "delta": {"refusal": piece}}]})
    assembly.add({"choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})

    choices = assembly.choices()
    assert choices[0]["message"]["refusal"] == "I can't help with that."
    assert choices[0]["message"]["content"] is None
    assert choices[0]["finish_reason"] == "stop"


def test_the_gateway_says_which_provider_answered():
    """An OpenAI-shaped reply names no provider, so a caller could only
    fall back to the manifest's packaged default — wrong for exactly the
    tenants who overrode it, which is the feature this batch adds. The
    resolved step comes back in the platform's own envelope."""
    from gateway.main import _resolved
    from gateway.steps import ResolvedStep

    step = ResolvedStep(step_id="think", provider="google", model="gemini-2.0-flash")
    out = _resolved({"id": "resp-1", "choices": []}, step)

    assert out["librerun"]["provider"] == "google", (
        "the envelope must carry the admin's own word, not LiteLLM's "
        "routing prefix — this is what an audit row and the config page show"
    )
    assert out["librerun"]["model"] == "gemini-2.0-flash"
    assert out["librerun"]["step_id"] == "think"
    # Still an OpenAI response: the envelope adds, it does not replace.
    assert out["id"] == "resp-1"


# --------------------------------------------------------------------------
# Audio: the fifth field that carries the answer
# --------------------------------------------------------------------------


AUDIO_MESSAGE = {
    "role": "assistant",
    "content": None,
    "audio": {
        "id": "audio_abc",
        "data": "QUJD" * 100,
        "transcript": f"The caller was {NAME}.",
        "expires_at": 1234567890,
    },
}


def test_an_audio_completion_is_not_recorded_as_an_empty_choice():
    """`modalities`/`audio` are forwarded on purpose, so an audio-only
    reply is a shape this gateway invites. Its answer is in
    `message.audio` with `content` null."""
    walked = telemetry.walked_choices(
        {"choices": [{"index": 0, "finish_reason": "stop", "message": AUDIO_MESSAGE}]}
    )
    assert walked[0].get("audio"), "the span recorded an audio reply as nothing"
    assert walked[0]["audio"]["transcript"]


def test_the_transcript_is_walked_and_the_payload_is_only_measured():
    """The transcript is text the model produced, so it is redacted like
    any other. The base64 `data` is megabytes of unredactable payload —
    the span keeps its size, never the bytes."""
    walked = telemetry.walked_choices(
        {"choices": [{"index": 0, "message": AUDIO_MESSAGE}]}
    )
    audio = walked[0]["audio"]

    assert NAME not in json.dumps(walked), "an unwalked name reached the span"
    assert audio["bytes"] == len(AUDIO_MESSAGE["audio"]["data"])
    assert AUDIO_MESSAGE["audio"]["data"] not in json.dumps(walked)
    # A provider-chosen identifier is not stamped, like `gen_ai.response.id`.
    assert "audio_abc" not in json.dumps(walked)


def test_a_replayed_audio_turn_is_recorded_on_the_prompt_side_too():
    walked = telemetry.walked_messages([AUDIO_MESSAGE])
    assert walked[0].get("audio", {}).get("transcript")


def test_a_streamed_audio_reply_is_assembled():
    """Decision 53's rule again: recorded on one side means recorded on
    both, or streamed and non-streamed disagree about the same reply."""
    from gateway.main import _StreamAssembly

    assembly = _StreamAssembly()
    for piece, data in (("The caller ", "QUJD"), ("was Margaret.", "RUZH")):
        assembly.add(
            {"choices": [{"index": 0, "delta": {"audio": {"transcript": piece, "data": data}}}]}
        )
    assembly.add({"choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]})

    message = assembly.choices()[0]["message"]
    assert message["audio"]["transcript"] == "The caller was Margaret."
    assert message["audio"]["bytes"] == 8

    # And the walker measures the assembled count without needing the
    # payload rebuilt: handing it a filler string of the right length
    # would allocate megabytes to learn a number already in hand.
    walked = telemetry.walked_choices({"choices": [{"index": 0, "message": message}]})
    assert walked[0]["audio"]["bytes"] == 8
    assert "Margaret" not in json.dumps(walked)
