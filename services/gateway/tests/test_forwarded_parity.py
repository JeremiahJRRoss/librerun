"""Every parameter the gateway forwards is one the stub must honour or refuse.

Codex has now found this same defect four times, in four places, one at
a time: the `forwarded()` allowlist (decision 72), the deadline scope
(79), the embedding `dimensions` (84), and — in one round — the chat
`n` and the embedding `encoding_format` (85). Each was found by a
reviewer reading one function. Nothing in the tree could have told us.

The shape is always the same. Keyless mode exists to prove the wiring,
so a parameter the gateway hands to a real provider and the stub quietly
drops is wiring keyless mode does NOT prove: the agent is developed
against one contract and deployed against another, and the divergence
surfaces on the day a credential is added or taken away — the worst
possible day to discover it.

So the decision is made here, for every forwarded name, and the build
fails if a name is added to either allowlist without one. The rule the
table applies:

    Honour or refuse any parameter that makes a real provider ADD a
    FIELD the caller then reads.
    Document one that only changes the values inside fields that are
    present either way.

The asymmetry is the whole of it: an ABSENT field breaks a reader — a
KeyError, a null dereference, a wait for something that never arrives —
while an extra field it did not ask for breaks nothing. So `n` (the
length of `choices`), `tools` (`message.tool_calls`), `logprobs`
(`choice.logprobs`) and `modalities: ["…","audio"]` (`message.audio`)
are all this table's business, and `top_p` is not.

The first version of this rule said "honour what changes control flow,
document what only changes detail", and it was too vague to apply twice
the same way: I marked `modalities`/`audio` IGNORED under it and a
reviewer had to point out that an audio consumer decoding an absent
field is not "told less" (Codex P2). Applying the sharper question —
*did the caller ask for a field, and is it missing?* — to the whole
table immediately reclassified `logprobs` and `top_logprobs` too, which
nobody had reported. A rule you cannot apply mechanically is a rule you
will apply inconsistently.

Between honouring and refusing, the split is what a fixture can stand
in for honestly. `logprobs` it can: the reply's own text is the token
sequence. Audio it cannot, and bytes shaped like a wav that decode to
nothing would fail in the caller's decoder rather than here — a worse
answer than a refusal, because the caller carries them somewhere first.

"honoured" is not a label here. Every honoured name carries a PROBE
below that proves the stub's reply actually changes with it, so the
table cannot drift into a comfortable fiction — which is the failure
mode of every catalogue that is only a catalogue.
"""
from __future__ import annotations

import base64
import json
import struct

import pytest

from gateway import errors, stub_provider
from gateway.egress import CHAT_PARAMS, EMBEDDING_PARAMS

HONOURED = "honoured"
REFUSED = "refused"
IGNORED = "ignored"


def _chat(**body):
    body.setdefault("messages", [{"role": "user", "content": "hi"}])
    return stub_provider.complete(body, model="stub", scenario=None)


def _content(completion) -> str:
    return completion["choices"][0]["message"].get("content") or ""


# --- the probes -------------------------------------------------------
# Each proves the stub's reply depends on its parameter. A probe that
# stops biting is a parameter that stopped being honoured.

def _probe_messages():
    assert _content(_chat(messages=[{"role": "user", "content": "a"}])) != _content(
        _chat(messages=[{"role": "user", "content": "b"}])
    )


def _probe_tools():
    forced = _chat(
        tools=[{"type": "function", "function": {"name": "look", "parameters": {}}}],
        tool_choice={"type": "function", "function": {"name": "look"}},
    )
    call = forced["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "look"
    assert forced["choices"][0]["finish_reason"] == "tool_calls"


def _probe_functions():
    """The legacy spelling forces a call too, or a client using it waits
    for a `function_call` that never comes."""
    forced = _chat(
        functions=[{"name": "look", "parameters": {}}],
        function_call={"name": "look"},
    )
    message = forced["choices"][0]["message"]
    assert message["function_call"]["name"] == "look"
    assert "tool_calls" not in message
    assert forced["choices"][0]["finish_reason"] == "function_call"


def _probe_response_format():
    reply = _chat(
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "x",
                "schema": {
                    "type": "object",
                    "properties": {"n": {"type": "integer", "minimum": 7}},
                    "required": ["n"],
                },
            },
        }
    )
    assert json.loads(_content(reply))["n"] >= 7


def _probe_n():
    reply = _chat(n=3)
    assert [c["index"] for c in reply["choices"]] == [0, 1, 2]
    # A provider samples `n` times; `n` copies of one fixture would still
    # misrepresent it to a caller that ranks or dedupes them.
    assert len({c["message"]["content"] for c in reply["choices"]}) == 3


def _probe_stream():
    reply = _chat(n=2)
    chunks = list(stub_provider.stream(reply))
    assert sorted({c["index"] for k in chunks for c in k["choices"]}) == [0, 1]
    assert sum("usage" in k for k in chunks) == 1


def _probe_seed():
    assert _content(_chat(seed=1)) != _content(_chat(seed=2))


def _probe_input():
    first = stub_provider.embed({"input": "a"}, model="stub")
    second = stub_provider.embed({"input": "b"}, model="stub")
    assert first["data"][0]["embedding"] != second["data"][0]["embedding"]


def _probe_dimensions():
    got = stub_provider.embed({"input": "a", "dimensions": 64}, model="stub")
    assert len(got["data"][0]["embedding"]) == 64


def _probe_encoding_format():
    packed = stub_provider.embed(
        {"input": "a", "encoding_format": "base64"}, model="stub"
    )["data"][0]["embedding"]
    plain = stub_provider.embed({"input": "a"}, model="stub")["data"][0]["embedding"]
    assert isinstance(packed, str) and isinstance(plain, list)
    # base64 is the raw little-endian float32 buffer — the same vector,
    # in the form the official client decodes.
    unpacked = struct.unpack(f"<{len(plain)}f", base64.b64decode(packed, validate=True))
    assert unpacked == pytest.approx(plain, abs=1e-6)


def _probe_logprobs():
    """A caller that asks for logprobs reads `choice.logprobs`, and the
    stub answered null — the same absent-field break as audio, which is
    why the sharper rule catches both."""
    reply = _chat(logprobs=True, top_logprobs=2)
    entries = reply["choices"][0]["logprobs"]["content"]
    assert entries, "logprobs was requested and the choice carries none"
    first = entries[0]
    assert set(first) >= {"token", "logprob", "bytes", "top_logprobs"}
    assert first["logprob"] < 0
    assert len(first["top_logprobs"]) == 2
    assert bytes(first["bytes"]).decode() == first["token"]
    # Not volunteered when it was not asked for: an extra field harms no
    # reader, but a provider does not send one either.
    assert _chat()["choices"][0]["logprobs"] is None
    # And the STREAMED form carries them too. `complete()` filled this
    # in and `stream()` dropped it, which is the `n` split exactly — the
    # non-streaming path fixed, the streamed one not, one round later
    # and inside the fix for it.
    streamed = [c for k in stub_provider.stream(reply) for c in k["choices"]]
    carried = [c for c in streamed if c.get("logprobs") is not None]
    assert carried, "logprobs survive the non-streamed reply but not the stream"
    assert carried[0]["logprobs"]["content"][0]["token"] == entries[0]["token"]


def _probe_modalities():
    """The stub cannot synthesise audio, so it says so rather than
    answering text-only and leaving `message.audio` absent."""
    with pytest.raises(errors.GatewayError) as caught:
        _chat(modalities=["text", "audio"], audio={"voice": "alloy", "format": "wav"})
    assert caught.value.code == "provider_refused"
    assert caught.value.param == "modalities"
    # Text-only is not an audio request and must still work.
    assert _chat(modalities=["text"])["choices"][0]["finish_reason"] == "stop"


# --- the tables -------------------------------------------------------

CHAT_PARITY: dict[str, tuple[str, str]] = {
    "messages": (HONOURED, "the prompt itself: it seeds the reply"),
    "tools": (HONOURED, "a forced call is answered with that call"),
    "tool_choice": (HONOURED, "names the tool to force"),
    "functions": (HONOURED, "the legacy spelling of tools"),
    "function_call": (HONOURED, "the legacy spelling of tool_choice"),
    "response_format": (HONOURED, "a schema is synthesised and returned"),
    "n": (HONOURED, "n indexed choices, as a provider would sample n times"),
    "stream": (HONOURED, "the reply arrives as chunks, every choice indexed"),
    "seed": (HONOURED, "part of the fingerprint, so it selects the fixture"),
    "parallel_tool_calls": (
        IGNORED,
        "the stub forces at most one call, so there is nothing to run in "
        "parallel; a client cannot observe an ordering that never exists",
    ),
    "stop": (
        IGNORED,
        "a fixture never contains the caller's stop string, so truncating "
        "on it is a no-op rather than a difference",
    ),
    "stream_options": (
        IGNORED,
        "include_usage only decides whether usage is OMITTED from the "
        "final chunk; the stub always sends it, and an extra field the "
        "caller did not ask for breaks no reader — the asymmetry the "
        "rule above turns on",
    ),
    "top_p": (IGNORED, "a sampling knob, and a fixture is not sampled"),
    "presence_penalty": (IGNORED, "a sampling knob, and a fixture is not sampled"),
    "frequency_penalty": (IGNORED, "a sampling knob, and a fixture is not sampled"),
    "logit_bias": (IGNORED, "a sampling knob, and a fixture is not sampled"),
    "logprobs": (
        HONOURED,
        "fills choice.logprobs, which the caller that asked for it reads; "
        "the reply's own text is the token sequence, so a fixture can "
        "stand in for this honestly",
    ),
    "top_logprobs": (
        HONOURED,
        "how many alternatives each token carries; scored like the tokens "
        "themselves, and refused outside the 0-20 a provider accepts",
    ),
    "modalities": (
        REFUSED,
        "asking for audio makes a provider fill message.audio, and the "
        "stub cannot synthesise audio — fabricated bytes would fail in "
        "the caller's decoder rather than here, so it refuses by name. "
        "A text-only modalities list is not an audio request and passes",
    ),
    "audio": (
        REFUSED,
        "the voice and format for the spoken reply that modalities asks "
        "for; that request is refused, so this never configures anything "
        "the stub produces",
    ),
    "prediction": (
        IGNORED,
        "a latency optimisation: the caller asks for a speedup, not for a "
        "field, and does not then branch on the accepted-token counts a "
        "provider reports back",
    ),
    "reasoning_effort": (
        IGNORED,
        "how hard a reasoning model thinks; a fixture does not think",
    ),
    "metadata": (IGNORED, "carried for the provider's own accounting"),
    "user": (IGNORED, "carried for the provider's own abuse handling"),
}

EMBEDDING_PARITY: dict[str, tuple[str, str]] = {
    "input": (HONOURED, "the text being embedded: it seeds the vector"),
    "dimensions": (HONOURED, "the vector comes back at the requested width"),
    "encoding_format": (HONOURED, "float or base64, as asked"),
    "user": (IGNORED, "carried for the provider's own abuse handling"),
}

PROBES = {
    "messages": _probe_messages,
    "tools": _probe_tools,
    "tool_choice": _probe_tools,
    "functions": _probe_functions,
    "function_call": _probe_functions,
    "response_format": _probe_response_format,
    "n": _probe_n,
    "stream": _probe_stream,
    "logprobs": _probe_logprobs,
    "top_logprobs": _probe_logprobs,
    "modalities": _probe_modalities,
    "audio": _probe_modalities,
    "seed": _probe_seed,
    "input": _probe_input,
    "dimensions": _probe_dimensions,
    "encoding_format": _probe_encoding_format,
}


@pytest.mark.parametrize(
    "allowlist, table, name",
    [
        (CHAT_PARAMS, CHAT_PARITY, "CHAT_PARAMS"),
        (EMBEDDING_PARAMS, EMBEDDING_PARITY, "EMBEDDING_PARAMS"),
    ],
)
def test_every_forwarded_parameter_has_a_decided_keyless_behaviour(
    allowlist, table, name
):
    """The gate: forwarding a parameter means deciding what keyless does
    with it. Not implementing it — DECIDING, in writing, where the next
    author will read it."""
    undecided = sorted(set(allowlist) - set(table))
    assert not undecided, (
        f"{name} forwards these to a real provider and nothing here says "
        f"what keyless mode does with them, so the stub and a credentialled "
        f"deployment may already disagree: {undecided}. Add a row to the "
        f"table — 'honoured' with a probe, or 'ignored' with the reason it "
        f"changes detail rather than control flow."
    )
    stale = sorted(set(table) - set(allowlist))
    assert not stale, (
        f"these carry a parity decision but {name} no longer forwards them, "
        f"so the row describes nothing: {stale}"
    )


@pytest.mark.parametrize("table", [CHAT_PARITY, EMBEDDING_PARITY])
def test_every_row_states_its_reasoning(table):
    for parameter, (status, why) in table.items():
        assert status in (HONOURED, REFUSED, IGNORED), (parameter, status)
        assert len(why) > 20, f"{parameter!r} gives no reason worth reading"


@pytest.mark.parametrize(
    "parameter",
    sorted(
        {k for k, (s, _) in CHAT_PARITY.items() if s in (HONOURED, REFUSED)}
        | {k for k, (s, _) in EMBEDDING_PARITY.items() if s in (HONOURED, REFUSED)}
    ),
)
def test_a_parameter_claimed_honoured_or_refused_is_actually_either(parameter):
    """Both claims are claims, and this is what makes them ones. A
    catalogue that only catalogues drifts into fiction the first time
    someone edits the code and not the table — and `ignored` is the only
    status needing no evidence, which is exactly why it is the one a
    tired author reaches for."""
    probe = PROBES.get(parameter)
    assert probe is not None, (
        f"{parameter!r} claims the stub honours or refuses it with nothing "
        f"to show for it; add a probe that fails if that stops being true"
    )
    probe()


def test_an_unknown_encoding_format_is_refused_rather_than_guessed():
    """The other half of the rule: a provider that cannot honour a
    parameter says so. Quietly serving floats for an unknown format is
    the same defect as quietly serving floats for base64."""
    with pytest.raises(errors.GatewayError) as caught:
        stub_provider.embed({"input": "a", "encoding_format": "wobble"}, model="stub")
    assert caught.value.code == "provider_refused"
    assert caught.value.param == "encoding_format"


@pytest.mark.parametrize("bad", [0, -1, True, 10**6, "3", 1.5])
def test_an_n_no_provider_would_serve_is_refused(bad):
    with pytest.raises(errors.GatewayError) as caught:
        _chat(n=bad)
    assert caught.value.code == "provider_refused"
    assert caught.value.param == "n"


def test_the_usage_counts_every_choice():
    """A provider bills for every completion it sampled, and the span's
    cost is computed from this. One choice's tokens for three choices'
    work would understate a keyless run by two thirds."""
    one = _chat()
    three = _chat(n=3)
    assert three["usage"]["completion_tokens"] > one["usage"]["completion_tokens"]
    assert three["usage"]["total_tokens"] == (
        three["usage"]["prompt_tokens"] + three["usage"]["completion_tokens"]
    )


def test_a_request_without_n_answers_exactly_as_it_did():
    """The compatibility half: reading `n` must not disturb the reply of
    every request that does not send one."""
    reply = _chat()
    assert len(reply["choices"]) == 1
    assert reply["choices"][0]["index"] == 0
    chunks = list(stub_provider.stream(reply))
    assert len(chunks) == 2
    assert "usage" in chunks[-1]
