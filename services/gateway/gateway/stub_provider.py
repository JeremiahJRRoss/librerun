"""The ``stub`` provider: keyless mode, inside the gateway (D4).

``LIBRERUN_STUB_LLM=true`` makes ``stub`` the provider for every step,
so the demo and CI drive real runs — intake, both phases, the approval
gate, the report, the trace — on a machine with no provider account
anywhere. Only the provider boundary is answered from fixtures; the
chassis, the gateway, the redaction, the span and the cost are all the
real ones, which is the whole point: mocking the platform would prove
something about the mock.

Replies are deterministic and chosen in this order:

1. an ``X-LibreRun-Scenario`` header naming a script in
   ``services/gateway/stub/`` — how a test asks for a *particular*
   reply, such as one carrying the PII fixture so the telemetry walk
   can be caught out;
2. the caller's own ``librerun.stub_reply``, when it sent one. An agent
   that ships curated keyless fixtures keeps them where they belong —
   in the agent — instead of the platform carrying content for somebody
   else's agent (L13). It is honoured **only** when the resolved
   provider is the stub, so it is inert in a credentialled deployment
   and cannot make the gateway answer for a provider it did call;
3. the request's own ``response_format`` schema, synthesised — so an
   agent that asks for structured output gets something its normalizer
   accepts even with no fixture of its own;
4. a forced tool call, when ``tool_choice`` names one, with arguments
   synthesised from that tool's parameters;
5. otherwise a marked reply keyed by the hash of the input, so the same
   conversation always gets the same answer.

Every generated string says it is a fixture.
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
import time
from pathlib import Path

import structlog

from gateway import errors
from gateway.synth import STUB_MARKER, SchemaTooLarge, Unsatisfiable, instance

logger = structlog.get_logger(__name__)

# The scripts live beside the package rather than inside it: they are
# data an operator or a test may add to, not code. In the image the
# package is at /app/gateway and the scripts at /app/stub; in the
# checkout they are services/gateway/{gateway,stub}. Both are the
# package's parent, so one expression finds them.
SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "stub"

# A stub reply still has to carry token counts, because the LLM span's
# usage attributes and its cost are part of what keyless mode is proving.
# Four characters to a token is the usual rule of thumb and is honest
# enough for a fixture.
_CHARS_PER_TOKEN = 4

# Embedding width. Small on purpose: deterministic vectors exist so
# kb_search returns stable results keyless, not so they are useful.
EMBEDDING_DIMENSIONS = 16


def _fingerprint(body: dict) -> str:
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()
    ).hexdigest()


def load_script(name: str) -> dict | None:
    """A named reply script, or None. The name is a single path segment;
    anything else is refused rather than resolved, so a header cannot
    read a file outside the directory."""
    candidate = (name or "").strip()
    if not candidate or "/" in candidate or "\\" in candidate or candidate.startswith("."):
        return None
    path = SCRIPTS_DIR / f"{candidate}.json"
    try:
        if path.parent != SCRIPTS_DIR or not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(
            "stub_script_unreadable", script=candidate, error=str(exc)
        )
        return None


def _estimate_tokens(value) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _prompt_tokens(body: dict) -> int:
    return sum(_estimate_tokens(m) for m in (body.get("messages") or [])) or 1


def _forced_call(body: dict):
    """The function this request insists on, and which spelling asked
    for it: ``(function, legacy)``, or None when nothing is forced.

    The legacy half is here because ``functions`` and ``function_call``
    are both in CHAT_PARAMS — a real provider honours them and comes
    back with a ``function_call`` and ``finish_reason:
    "function_call"``, while keyless answered with a plain sentence and
    ``"stop"``. A client that forces a call and then waits for one does
    not get less detail keyless, it gets a different control flow: it
    waits for something that never arrives. That is the line this
    module draws — honour what changes an agent's control flow,
    document what only changes detail (see ``CHAT_PARITY`` in the
    tests). The redaction walk already handles both spellings "for the
    same reason: clients still send them".
    """
    tools = [t for t in (body.get("tools") or []) if isinstance(t, dict)]
    if tools:
        choice = body.get("tool_choice")
        if isinstance(choice, dict):
            wanted = ((choice.get("function") or {}).get("name")) or ""
            for tool in tools:
                if (tool.get("function") or {}).get("name") == wanted:
                    return (tool.get("function") or {}), False
            return None
        if choice == "required":
            return (tools[0].get("function") or {}), False
        return None

    functions = [f for f in (body.get("functions") or []) if isinstance(f, dict)]
    if not functions:
        return None
    # The legacy spelling names its function directly and has no
    # "required": only the object form forces a call.
    choice = body.get("function_call")
    if isinstance(choice, dict):
        wanted = choice.get("name") or ""
        for function in functions:
            if function.get("name") == wanted:
                return function, True
    return None


def _supplied_reply(body: dict) -> dict | None:
    """The caller's own canned reply, when it sent one."""
    extension = body.get("librerun")
    if not isinstance(extension, dict):
        return None
    reply = extension.get("stub_reply")
    if reply is None:
        return None
    content = reply if isinstance(reply, str) else json.dumps(reply, default=str)
    return {"role": "assistant", "content": content}


def _message(body: dict, *, seed: str) -> dict:
    """The assistant message the stub answers with."""
    supplied = _supplied_reply(body)
    if supplied is not None:
        return supplied
    forced = _forced_call(body)
    if forced is not None:
        function, legacy = forced
        arguments = _synthesised(
            function.get("parameters") or {}, seed=seed, path="$.arguments"
        )
        name = function.get("name") or "stub_tool"
        if legacy:
            return {
                "role": "assistant",
                "content": None,
                "function_call": {"name": name, "arguments": json.dumps(arguments)},
            }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_stub_{seed[:12]}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }

    schema = (
        ((body.get("response_format") or {}).get("json_schema") or {}).get("schema")
        if isinstance(body.get("response_format"), dict)
        else None
    )
    if isinstance(schema, dict) and schema:
        return {
            "role": "assistant",
            "content": json.dumps(_synthesised(schema, seed=seed)),
        }
    if (body.get("response_format") or {}).get("type") == "json_object":
        return {
            "role": "assistant",
            "content": json.dumps({"answer": f"{STUB_MARKER} {seed[:8]}"}),
        }
    return {"role": "assistant", "content": f"{STUB_MARKER} {seed[:8]}"}


def _finish_reason(message: dict) -> str:
    """How the model stopped, in the spelling that matches the reply.

    A legacy ``function_call`` ends with ``"function_call"``, not
    ``"tool_calls"`` and not ``"stop"`` — a client switching on this
    field is the same control flow the call itself is part of.
    """
    if message.get("tool_calls"):
        return "tool_calls"
    if message.get("function_call"):
        return "function_call"
    return "stop"


def _script_message(script: dict, seed: str) -> dict:
    message = {"role": "assistant", "content": script.get("content")}
    calls = script.get("tool_calls")
    if isinstance(calls, list) and calls:
        message["tool_calls"] = [
            {
                "id": call.get("id") or f"call_stub_{seed[:12]}",
                "type": "function",
                "function": {
                    "name": (call.get("function") or {}).get("name", "stub_tool"),
                    "arguments": json.dumps(
                        (call.get("function") or {}).get("arguments", {})
                    )
                    if not isinstance(
                        (call.get("function") or {}).get("arguments"), str
                    )
                    else (call.get("function") or {}).get("arguments"),
                },
            }
            for call in calls
            if isinstance(call, dict)
        ]
    return message


def _width(body: dict) -> int:
    """The vector width this request asked for, or the default.

    A provider refuses a `dimensions` it cannot serve, so the stub —
    which stands in for one — refuses rather than quietly answering at
    some other width. Silently substituting is the failure this whole
    parameter is about.
    """
    wanted = body.get("dimensions")
    if wanted is None:
        return EMBEDDING_DIMENSIONS
    if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted < 1:
        raise errors.bad_request(
            "provider_refused",
            "'dimensions' must be a positive integer",
            param="dimensions",
        )
    if wanted > MAX_EMBEDDING_DIMENSIONS:
        raise errors.bad_request(
            "provider_refused",
            f"keyless mode synthesises at most {MAX_EMBEDDING_DIMENSIONS} "
            f"dimensions; a real provider decides its own limit",
            param="dimensions",
        )
    return wanted


# How many completions one keyless request may ask for. OpenAI caps `n`
# at 128 and the stub stands in for a provider rather than out-doing
# one; the bound is here for the reason MAX_EMBEDDING_DIMENSIONS is —
# `n` arrives from the wire and every choice is work on the event loop.
MAX_CHOICES = 128


def _choice_count(body: dict) -> int:
    """How many choices this request asked for.

    `n` is in CHAT_PARAMS, so a credentialled deployment answers with
    `n` completions — and keyless answered with exactly one, always. An
    agent developed against the stub (picking the best of three, say)
    read `choices[0]` and worked in the demo, then met two more choices
    the day a key was added; one written the other way round raised
    IndexError on `choices[2]` the day a key was taken away. That is the
    divergence decisions 72, 79 and 84 are about, in a fourth place:
    every parameter the gateway forwards is one the stub must honour or
    refuse.
    """
    wanted = body.get("n")
    if wanted is None:
        return 1
    if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted < 1:
        raise errors.bad_request(
            "provider_refused", "'n' must be a positive integer", param="n"
        )
    if wanted > MAX_CHOICES:
        raise errors.bad_request(
            "provider_refused",
            f"keyless mode synthesises at most {MAX_CHOICES} choices; a real "
            f"provider decides its own limit",
            param="n",
        )
    return wanted


# The two encodings the OpenAI embeddings API defines.
_ENCODINGS = ("float", "base64")


def _encoding(body: dict) -> str:
    """The wire form this request asked its vectors in.

    ``encoding_format`` is in EMBEDDING_PARAMS, so a real provider
    honours it and the stub returned a list of floats whatever was
    asked. A client that requested ``base64`` — which the official
    OpenAI client does by default when numpy is installed — got
    something it could not decode, in keyless mode only. An unknown
    value is refused rather than quietly served as floats, for the
    reason ``_width`` refuses an impossible ``dimensions``: a provider
    that cannot honour a parameter says so.
    """
    wanted = body.get("encoding_format")
    if wanted is None:
        return "float"
    if wanted not in _ENCODINGS:
        raise errors.bad_request(
            "provider_refused",
            f"'encoding_format' must be one of {', '.join(_ENCODINGS)}",
            param="encoding_format",
        )
    return wanted


def _encoded(vector: list[float], encoding: str):
    """``vector`` in the requested wire form.

    ``base64`` is the raw little-endian float32 buffer, base64'd — what
    the OpenAI client decodes with
    ``numpy.frombuffer(..., dtype="float32")``. Packing exactly that is
    what makes the two formats interchangeable to a caller; note that
    the round trip is through float32, so a base64 vector differs from
    the float one in the last few digits, as it does from a provider.
    """
    if encoding == "float":
        return vector
    return base64.b64encode(struct.pack(f"<{len(vector)}f", *vector)).decode("ascii")


# OpenAI caps `top_logprobs` at 20.
MAX_TOP_LOGPROBS = 20


def _refuse_unsynthesisable(body: dict) -> None:
    """Refuse a reply this stub cannot honestly stand in for.

    ``modalities: ["text","audio"]`` makes a real provider answer with
    ``message.audio`` — an id, base64 audio and a transcript — and the
    caller then decodes it. Answering with text alone leaves that field
    absent, so the caller does not get a less detailed reply, it gets a
    KeyError or a wait for audio that never arrives (Codex P2).

    The stub cannot synthesise audio. Bytes shaped like a wav that
    decode to nothing would be a worse answer than a refusal, because
    the caller would carry them somewhere before finding out. So this is
    the other half of the rule the parity table states: **synthesise
    what a fixture can honestly stand in for, refuse what it cannot.**
    """
    modalities = body.get("modalities")
    if isinstance(modalities, list) and any(
        isinstance(m, str) and m.lower() == "audio" for m in modalities
    ):
        raise errors.bad_request(
            "provider_refused",
            "keyless mode synthesises text; it cannot stand in for an audio "
            "reply, and fabricated audio bytes would fail in your decoder "
            "rather than here. Ask a real provider for audio modalities.",
            param="modalities",
        )


def _logprob_for(token: str, index: int) -> float:
    """A deterministic, plausible log-probability. Always negative, and
    nearer zero for earlier tokens, which is what a real distribution
    usually looks like without pretending to be one."""
    digest = hashlib.sha256(f"{index}|{token}".encode()).digest()
    return -round((digest[0] / 255.0) * 1.5 + 0.01, 6)


def _logprobs(message: dict, body: dict) -> dict | None:
    """The per-token probabilities, when the caller asked for them.

    ``logprobs`` is forwarded, so a real provider fills
    ``choice.logprobs`` and the stub left it null — the same absent-field
    break as audio above, for a caller that reads it. Unlike audio, a
    fixture CAN stand in for this honestly: the reply's own text is the
    token sequence, and the numbers are marked as generated by being
    derived from it.
    """
    wanted = body.get("logprobs")
    if wanted is None or wanted is False:
        return None
    if not isinstance(wanted, bool):
        raise errors.bad_request(
            "provider_refused", "'logprobs' must be a boolean", param="logprobs"
        )
    alternatives = body.get("top_logprobs")
    if alternatives is None:
        alternatives = 0
    elif (
        not isinstance(alternatives, int)
        or isinstance(alternatives, bool)
        or not 0 <= alternatives <= MAX_TOP_LOGPROBS
    ):
        raise errors.bad_request(
            "provider_refused",
            f"'top_logprobs' must be an integer between 0 and "
            f"{MAX_TOP_LOGPROBS}",
            param="top_logprobs",
        )
    content = message.get("content")
    if not isinstance(content, str) or not content:
        # A tool call carries no content, and a provider returns a null
        # `content` list for it rather than an empty one.
        return {"content": None, "refusal": None}
    entries = []
    for index, word in enumerate(content.split(" ")):
        token = word if index == 0 else f" {word}"
        entries.append(
            {
                "token": token,
                "logprob": _logprob_for(token, index),
                "bytes": list(token.encode()),
                "top_logprobs": [
                    {
                        "token": token if rank == 0 else f"{token}{rank}",
                        "logprob": _logprob_for(token, index + rank),
                        "bytes": list(
                            (token if rank == 0 else f"{token}{rank}").encode()
                        ),
                    }
                    for rank in range(alternatives)
                ],
            }
        )
    return {"content": entries, "refusal": None}


def _synthesised(schema, *, seed: str, path: str = "$"):
    """``synth.instance``, with its refusal translated.

    ``synth`` raises its own exception because it synthesises JSON and
    knows nothing about HTTP — the same separation ``redaction`` keeps
    for ``_Refusal``. This is the one place that knows both.
    """
    try:
        return instance(schema, seed=seed, path=path)
    except SchemaTooLarge as refusal:
        raise errors.bad_request(
            "schema_too_large",
            f"the schema at {refusal.path} asks for more values than keyless "
            f"mode will synthesise. The stub answers with a FIXTURE shaped "
            f"like your schema, not a dataset; bound the array (maxItems) or "
            f"ask a real provider.",
            param=refusal.path,
        ) from None
    except Unsatisfiable as refusal:
        raise errors.bad_request(
            "schema_unsatisfiable",
            f"the schema at {refusal.path} excludes every value its own "
            f"bounds allow, so no reply can satisfy it — a real provider "
            f"would answer and your validator would reject the answer. "
            f"Check the exclusive bounds against the declared type.",
            param=refusal.path,
        ) from None


def complete(body: dict, *, model: str, scenario: str | None = None) -> dict:
    """An OpenAI ``chat.completion`` the caller cannot tell from a real
    one, except that it says so."""
    _refuse_unsynthesisable(body)
    seed = _fingerprint(body)
    script = load_script(scenario) if scenario else None
    choices = []
    completion_tokens = 0
    for index in range(_choice_count(body)):
        # A per-choice seed. A real provider SAMPLES `n` times and comes
        # back with `n` different completions, so `n` identical fixtures
        # would still misrepresent it to a caller that ranks or dedupes
        # them. Derived from the request, so the reply stays
        # reproducible; a request that sends no `n` at all keeps the
        # bare seed and answers byte-for-byte as it did before `n` was
        # read. (A request that DOES send one already has a different
        # fingerprint — `n` is part of the body being hashed — so its
        # choice 0 differs, exactly as a different request should.)
        #
        # Under a scenario SCRIPT the contents are identical whatever
        # the salt, and deliberately: a test that asked for a particular
        # reply wants that reply, n times, not n variations on it. The
        # salt still separates their tool-call ids.
        #
        # Re-hashed rather than suffixed. `f"{seed}#{index}"` was the
        # obvious spelling and it produced `n` IDENTICAL choices: the
        # fixture renders `seed[:8]`, and appending to the end leaves
        # the first eight characters exactly where they were. The
        # comment above was false for as long as that line stood, which
        # is the whole argument for checking a claim instead of writing
        # it.
        salt = (
            seed
            if index == 0
            else hashlib.sha256(f"{seed}#{index}".encode()).hexdigest()
        )
        message = (
            _script_message(script, salt) if script else _message(body, seed=salt)
        )
        completion_tokens += _estimate_tokens(
            message.get("content") or message.get("tool_calls") or ""
        )
        choices.append(
            {
                "index": index,
                "message": message,
                "finish_reason": _finish_reason(message),
                "logprobs": _logprobs(message, body),
            }
        )
    prompt_tokens = _prompt_tokens(body)
    return {
        "id": f"chatcmpl-stub-{seed[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": choices,
        "usage": {
            # Every choice, because a provider bills for every choice —
            # and the span's cost is computed from this.
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "librerun_stub": True,
    }


def stream(completion: dict):
    """The same reply as OpenAI streaming chunks.

    A framework that asks for ``stream: true`` must get a stream, or
    keyless mode would only work for agents that do not. The content
    arrives in one chunk plus the terminator — a stub has nothing to
    gain from pretending to think slowly.

    EVERY choice, not only the first. ``n`` and ``stream`` are both
    forwarded, so a credentialled stream carries `n` interleaved
    ``index`` values and the gateway's own assembly (``main._Assembly``)
    already keys on them; emitting index 0 alone made keyless streaming
    disagree with keyless non-streaming as well as with a provider.
    """
    base = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }
    choices = completion["choices"]
    for choice in choices:
        message = choice["message"]
        delta: dict = {"role": "assistant"}
        if message.get("content") is not None:
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = [
                {"index": i, **call} for i, call in enumerate(message["tool_calls"])
            ]
        if message.get("function_call"):
            # The legacy spelling streams under its own key, which the
            # gateway's assembly already reassembles (``entry["legacy"]``).
            delta["function_call"] = message["function_call"]
        streamed = {"index": choice["index"], "delta": delta, "finish_reason": None}
        if choice.get("logprobs") is not None:
            # The content arrives in one chunk, so its probabilities ride
            # that chunk. `complete()` filled this in and `stream()`
            # dropped it on the floor — the SAME split as `n`, where the
            # non-streaming path was fixed and the streamed one was not,
            # one round later and in the fix for it (Codex P2).
            streamed["logprobs"] = choice["logprobs"]
        yield {**base, "choices": [streamed]}
    for position, choice in enumerate(choices):
        terminator = {
            **base,
            "choices": [
                {
                    "index": choice["index"],
                    "delta": {},
                    "finish_reason": choice["finish_reason"],
                }
            ],
        }
        if position == len(choices) - 1:
            # Usage is the whole reply's, so it rides the last chunk —
            # which for a single choice is the shape this always had.
            terminator["usage"] = completion["usage"]
        yield terminator


# What the stub will synthesise. Not a policy about real providers —
# they validate `dimensions` themselves — only a bound on the work one
# keyless request can ask this process to do.
MAX_EMBEDDING_DIMENSIONS = 8192


def embed(body: dict, *, model: str) -> dict:
    """Deterministic vectors, so keyless ``kb_search`` gives stable
    results rather than noise that changes every run.

    ``dimensions`` is honoured. It is a parameter the gateway forwards
    to a real provider, and the stub used to ignore it and always emit
    sixteen — so a client written against `dimensions: 256` got the
    wrong shape in the demo and a different vector contract the moment
    credentials were enabled, and keyless `kb_search` could not match a
    Pinecone index whose model is not 16-dimensional (Codex P2). The
    same divergence decisions 72 and 79 are about, in a third place.

    ``encoding_format`` is honoured for the same reason — see
    ``_encoding``.
    """
    raw = body.get("input")
    inputs = raw if isinstance(raw, list) else [raw]
    width = _width(body)
    encoding = _encoding(body)
    data = []
    total = 0
    for index, value in enumerate(inputs):
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        total += _estimate_tokens(text)
        digest = hashlib.sha256(text.encode()).digest()
        vector = [
            (digest[i % len(digest)] / 255.0) * 2 - 1 for i in range(width)
        ]
        data.append(
            {
                "object": "embedding",
                "index": index,
                "embedding": _encoded(vector, encoding),
            }
        )
    return {
        "object": "list",
        "data": data,
        "model": model,
        "usage": {"prompt_tokens": total, "total_tokens": total},
        "librerun_stub": True,
    }
