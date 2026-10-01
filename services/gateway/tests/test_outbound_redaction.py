"""Nothing the model reads leaves the box unexamined (blueprint S4a).

Two kinds of test here. The first kind names the positions the Accept
list names, one per case, and asserts the exact outcome. The second is
the **totality** test: it walks a maximal request, finds every string
position, every number position and every object key by construction,
and fails if any of them reaches the provider neither redacted nor
refused. The first kind says what the rule is; the second says there is
no position the rule forgot.

The fixture is an email address of the same shape the intake tests use,
and the assertions compare against the pipeline's own output rather than
a literal placeholder that would drift.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.services import pii_service

from gateway import errors
from gateway.redaction import opaque_user, redact_request

# The value injected everywhere. An email is what the intake fixtures
# carry, it is caught by the redactor's stages AND by the identifier
# rule, and it survives json round-trips.
FIXTURE = "pii-test@example.com"

# A Luhn-valid card number, for the numeric positions. As a JSON number
# it cannot take a textual placeholder without changing its type.
CARD_NUMBER = 4111111111111111

# A person's name: caught by the redactor's NER stage and by NOTHING
# else. The email fixture above is caught by the identifier rule too, so
# a position that is merely CHECKED rather than rewritten still refuses
# it — which means the email cannot tell a rewritable position from an
# unrewritable one. Only a name can, and that is the difference Codex's
# P1 turned on: `functions` prose was reaching the identifier rule, and
# the identifier rule does not know a name when it sees one.
PERSON = "Dana Whitfield"


def placeholder_for(text: str) -> str:
    """What the pipeline itself produces for the fixture — never a
    literal, which would drift the moment _mask changes."""
    redacted, _ = pii_service.redact(text, quiet=True)
    return redacted


def run(body: dict, *, enabled: bool = True, embedding: bool = False):
    return redact_request(
        body, tenant_id="11111111-1111-1111-1111-111111111111",
        enabled=enabled, embedding=embedding,
    )


def refusal(body: dict, **kwargs) -> errors.GatewayError:
    with pytest.raises(errors.GatewayError) as exc:
        run(body, **kwargs)
    return exc.value


def chat(**overrides) -> dict:
    body = {
        "model": "librerun/think",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


# --------------------------- rewritable positions ---------------------------


def test_a_prompt_reaches_the_provider_redacted():
    body = chat(messages=[{"role": "user", "content": f"write to {FIXTURE}"}])

    out, report = run(body)

    assert FIXTURE not in json.dumps(out)
    assert out["messages"][0]["content"] == placeholder_for(f"write to {FIXTURE}")
    assert report.redacted_paths == ["$.messages[0].content"]


def test_a_text_part_is_redacted_like_a_string_content():
    body = chat(
        messages=[
            {
                "role": "user",
                "content": [{"type": "text", "text": f"write to {FIXTURE}"}],
            }
        ]
    )

    out, _ = run(body)

    assert FIXTURE not in json.dumps(out)
    assert out["messages"][0]["content"][0]["text"].startswith("write to [REDACTED")


def test_the_participant_name_is_dropped_rather_than_placeheld():
    """Providers constrain `name` to an identifier pattern no placeholder
    satisfies, so sending the placeholder would fail at the provider
    instead of here. The provider-side record shows the field absent."""
    body = chat(messages=[{"role": "user", "content": "hi", "name": FIXTURE}])

    out, report = run(body)

    assert "name" not in out["messages"][0]
    assert report.dropped_paths == ["$.messages[0].name"]
    # A name redaction would not change keeps its value.
    kept, _ = run(chat(messages=[{"role": "user", "content": "hi", "name": "agent_7"}]))
    assert kept["messages"][0]["name"] == "agent_7"


def test_a_tool_result_message_is_redacted():
    body = chat(
        messages=[
            {"role": "tool", "tool_call_id": "call_1", "content": f"found {FIXTURE}"}
        ]
    )

    out, _ = run(body)

    assert FIXTURE not in json.dumps(out)


def test_replayed_tool_call_arguments_are_redacted_and_stay_valid_json():
    arguments = json.dumps({"to": FIXTURE, "subject": "hi"})
    body = chat(
        messages=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "send_mail", "arguments": arguments},
                    }
                ],
            }
        ]
    )

    out, _ = run(body)

    sent = out["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert FIXTURE not in sent
    parsed = json.loads(sent)  # still valid JSON for the round trip
    assert parsed["to"] == placeholder_for(FIXTURE)
    assert parsed["subject"] == "hi"


def test_legacy_function_call_arguments_are_redacted():
    body = chat(
        messages=[
            {
                "role": "assistant",
                "function_call": {
                    "name": "send_mail",
                    "arguments": json.dumps({"to": FIXTURE}),
                },
            }
        ]
    )

    out, _ = run(body)

    assert FIXTURE not in json.dumps(out)


def test_arguments_that_do_not_parse_are_redacted_as_text():
    body = chat(
        messages=[
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "f", "arguments": f"not json {FIXTURE}"},
                    }
                ],
            }
        ]
    )

    out, _ = run(body)

    assert FIXTURE not in json.dumps(out)


def test_tool_description_and_schema_prose_are_redacted():
    body = chat(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": f"asks {FIXTURE}",
                    "parameters": {
                        "type": "object",
                        "title": f"Form for {FIXTURE}",
                        "properties": {
                            "q": {"type": "string", "description": f"like {FIXTURE}"}
                        },
                    },
                },
            }
        ]
    )

    out, _ = run(body)

    assert FIXTURE not in json.dumps(out)


@pytest.mark.parametrize("keyword", ["enum", "const", "examples", "default"])
def test_value_bearing_schema_keywords_are_redacted_recursively(keyword):
    """An agent that builds these from tenant rows would otherwise ship
    them. The value may itself be an object or an array, so the walk is
    recursive — the Accept names an `examples` entry that is an object
    carrying the value as a nested field."""
    value = {
        "enum": [f"a {FIXTURE}"],
        "const": f"c {FIXTURE}",
        "examples": [{"nested": {"deeper": [f"e {FIXTURE}"]}}],
        "default": f"d {FIXTURE}",
    }[keyword]
    body = chat(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "parameters": {
                        "type": "object",
                        "properties": {"q": {"type": "string", keyword: value}},
                    },
                },
            }
        ]
    )

    out, _ = run(body)

    assert FIXTURE not in json.dumps(out)


def test_response_format_prose_is_redacted():
    body = chat(
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "answer",
                "description": f"about {FIXTURE}",
                "schema": {"type": "object", "title": f"T {FIXTURE}"},
            },
        }
    )

    out, _ = run(body)

    assert FIXTURE not in json.dumps(out)


def test_an_embeddings_input_is_redacted_scalar_and_array():
    scalar, _ = run({"input": f"about {FIXTURE}"}, embedding=True)
    assert FIXTURE not in json.dumps(scalar)

    array, _ = run({"input": ["clean", f"about {FIXTURE}"]}, embedding=True)
    assert FIXTURE not in json.dumps(array)


def test_the_user_field_is_replaced_not_redacted():
    """Provider-side metadata rather than model input: replaced by a
    stable opaque per-tenant value, so the provider's own rate-limiting
    still works and the field carries nothing about a person."""
    out, _ = run(chat(user="alice@example.com"))

    assert out["user"] == opaque_user("11111111-1111-1111-1111-111111111111")
    assert "alice" not in json.dumps(out)
    # Stable for one tenant, different for another.
    again, _ = run(chat(user="someone-else"))
    assert again["user"] == out["user"]
    other, _ = redact_request(chat(user="x"), tenant_id="other", enabled=True)
    assert other["user"] != out["user"]


# -------------------------- unrewritable positions --------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(
            chat(
                tools=[
                    {"type": "function", "function": {"name": f"tool_{FIXTURE}"}}
                ]
            ),
            id="tools[].function.name",
        ),
        pytest.param(
            chat(
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": "call_1", "function": {"name": FIXTURE}}
                        ],
                    }
                ]
            ),
            id="tool_calls[].function.name",
        ),
        pytest.param(
            chat(
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"id": FIXTURE, "function": {"name": "f"}}
                        ],
                    }
                ]
            ),
            id="tool_calls[].id",
        ),
        pytest.param(
            chat(messages=[{"role": "tool", "tool_call_id": FIXTURE, "content": "x"}]),
            id="tool_call_id",
        ),
        pytest.param(
            chat(
                messages=[
                    {"role": "assistant", "function_call": {"name": FIXTURE}}
                ]
            ),
            id="function_call.name",
        ),
        pytest.param(
            chat(tool_choice={"type": "function", "function": {"name": FIXTURE}}),
            id="tool_choice.function.name",
        ),
        pytest.param(
            chat(
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": FIXTURE, "schema": {"type": "object"}},
                }
            ),
            id="response_format.json_schema.name",
        ),
        pytest.param(
            chat(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "f",
                            "parameters": {
                                "type": "object",
                                "properties": {FIXTURE: {"type": "string"}},
                            },
                        },
                    }
                ]
            ),
            id="schema property name",
        ),
        pytest.param(
            chat(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "f",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "q": {"examples": [{FIXTURE: "v"}]}
                                },
                            },
                        },
                    }
                ]
            ),
            id="object key under examples",
        ),
        pytest.param(
            chat(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "f",
                            "parameters": {
                                "type": "object",
                                "properties": {"q": {"pattern": FIXTURE}},
                            },
                        },
                    }
                ]
            ),
            id="pattern",
        ),
        pytest.param(
            chat(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "f",
                            "parameters": {
                                "type": "object",
                                "required": [FIXTURE],
                            },
                        },
                    }
                ]
            ),
            id="required entry",
        ),
        pytest.param(
            chat(
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {
                                    "name": "f",
                                    "arguments": json.dumps({FIXTURE: "v"}),
                                },
                            }
                        ],
                    }
                ]
            ),
            id="object key inside arguments",
        ),
    ],
)
def test_an_identifier_position_is_refused_with_nothing_leaving(body):
    error = refusal(body)

    assert error.status_code == 400
    assert error.code == "pii_in_identifier"
    # The refusal names the PATH and never the value.
    assert FIXTURE not in error.message
    assert FIXTURE not in (error.param or "")


# ------------------------------- numbers ------------------------------------


@pytest.mark.parametrize(
    "body,where",
    [
        (
            chat(
                messages=[
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "f",
                                    "arguments": json.dumps({"card": CARD_NUMBER}),
                                },
                            }
                        ],
                    }
                ]
            ),
            "arguments",
        ),
        (
            chat(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "f",
                            "parameters": {
                                "type": "object",
                                "properties": {"q": {"examples": [CARD_NUMBER]}},
                            },
                        },
                    }
                ]
            ),
            "examples",
        ),
        (
            chat(
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "f",
                            "parameters": {
                                "type": "object",
                                "properties": {"q": {"minimum": CARD_NUMBER}},
                            },
                        },
                    }
                ]
            ),
            "minimum",
        ),
    ],
)
def test_a_number_that_looks_like_personal_data_refuses_the_request(body, where):
    error = refusal(body)

    assert error.code == "pii_in_structured_value", where
    assert str(CARD_NUMBER) not in error.message


def test_ordinary_numbers_pass():
    out, _ = run(
        chat(
            max_tokens=2000,
            temperature=0.0,
            n=1,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "f",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "q": {"minimum": 0, "maximum": 100, "multipleOf": 5}
                            },
                        },
                    },
                }
            ],
        )
    )

    assert out["max_tokens"] == 2000
    assert out["tools"][0]["function"]["parameters"]["properties"]["q"]["maximum"] == 100


# ----------------------- what the gateway cannot read -----------------------


def test_a_media_part_is_refused_rather_than_forwarded():
    body = chat(
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
                ],
            }
        ]
    )

    error = refusal(body)

    assert error.code == "binary_not_redactable"
    # …and with the switch off it passes through unchanged.
    out, report = run(body, enabled=False)
    assert out == body and not report.changed


@pytest.mark.parametrize(
    "value", [[1, 2, 3], [[1, 2], [3, 4]]], ids=["token ids", "batched token ids"]
)
def test_token_id_embedding_input_is_refused(value):
    error = refusal({"input": value}, embedding=True)

    assert error.code == "tokens_not_redactable"
    out, _ = run({"input": value}, embedding=True, enabled=False)
    assert out == {"input": value}


def test_the_switch_off_sends_model_input_unchanged():
    """The manifest's explicit opt-out: the same request the switch-on
    run refuses passes through untouched, identifiers included."""
    body = chat(
        messages=[{"role": "user", "content": f"write to {FIXTURE}", "name": FIXTURE}],
        tools=[{"type": "function", "function": {"name": f"tool_{FIXTURE}"}}],
    )

    out, report = run(body, enabled=False)

    assert out == body
    assert FIXTURE in json.dumps(out)
    assert not report.changed


def test_a_person_named_in_a_predicted_output_is_redacted():
    """Predicted Outputs carry the text the caller expects back, which
    the model reads. In the structural default a name went out whole —
    the same shape as the `functions` gap, and reachable because the
    egress allowlist forwards this field."""
    out, _ = run(
        chat(prediction={"type": "content", "content": f"drafted by {PERSON}"})
    )

    assert PERSON not in json.dumps(out)
    assert placeholder_for(PERSON) in out["prediction"]["content"]


def test_the_switch_off_still_replaces_user():
    """``llm.redact_outbound`` governs what the MODEL reads. ``user`` is
    never read by a model: it is the handle the provider files the
    request under and keeps. An agent may opt itself out of redaction;
    it may not opt an end user into being registered with the provider
    under their own address.
    """
    out, report = run(chat(user=FIXTURE), enabled=False)

    assert out["user"] == opaque_user("11111111-1111-1111-1111-111111111111")
    assert FIXTURE not in json.dumps(out)
    # …and the rest of the body is still untouched, which is what the
    # opt-out is for.
    assert out["messages"] == chat()["messages"]
    assert not report.changed


def test_the_switch_off_leaves_a_body_with_no_user_alone():
    """The replacement must not INVENT the field: a request that never
    carried ``user`` would otherwise start identifying its tenant to the
    provider because redaction was turned off."""
    out, _ = run(chat(), enabled=False)

    assert "user" not in out


def test_user_is_replaced_the_same_way_with_the_switch_on_or_off():
    """One value, whichever way the switch is set — otherwise the
    provider's rate limiting sees two different tenants for one."""
    on, _ = run(chat(user="alice"))
    off, _ = run(chat(user="alice"), enabled=False)

    assert on["user"] == off["user"] == opaque_user(
        "11111111-1111-1111-1111-111111111111"
    )


def test_an_embedding_request_has_its_user_replaced_with_the_switch_off():
    """Embeddings take the same door and carry the same field."""
    out, _ = run({"input": ["hello"], "user": FIXTURE}, embedding=True, enabled=False)

    assert out["user"] == opaque_user("11111111-1111-1111-1111-111111111111")
    assert out["input"] == ["hello"]


# ------------------------------- totality -----------------------------------
#
# The rule is only worth anything if it is total, and "we listed the
# positions" is not a proof — the list is exactly the thing that goes
# stale. So: take a maximal request, find every string, every number and
# every object key BY WALKING IT, inject the fixture at one position at a
# time, and require that each one comes back either refused or with no
# trace of the fixture in what would have been sent. A position nobody
# thought about is found by the walk, not by remembering it.


def test_a_person_named_in_legacy_function_prose_is_redacted():
    """`functions` is the legacy `tools`, still accepted by the API, and
    its description and parameter prose are read by the model exactly
    the same way. Before this fix it fell through to the structural
    walk, where strings are CHECKED with the identifier rule and never
    rewritten — and that rule omits the NER stage on purpose, so a
    customer's name went to the provider verbatim with outbound
    redaction on (Codex P1).

    The email fixture cannot show this: the identifier rule catches an
    email, so the request was refused and the position looked safe. A
    name is the case that separates "checked" from "redacted".
    """
    body = chat(
        functions=[
            {
                "name": "lookup",
                "description": f"ask {PERSON} about the ticket",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "who": {"type": "string", "description": f"e.g. {PERSON}"}
                    },
                },
            }
        ]
    )

    out, _ = run(body)

    function = out["functions"][0]
    assert PERSON not in json.dumps(out)
    assert function["description"] == placeholder_for(f"ask {PERSON} about the ticket")
    assert function["parameters"]["properties"]["who"]["description"] == (
        placeholder_for(f"e.g. {PERSON}")
    )
    # The function's NAME is an identifier, so it is checked and kept.
    assert function["name"] == "lookup"


def test_the_two_spellings_of_a_tool_are_one_code_path():
    """`tools` and `functions` differ by a wrapper, so the same prose in
    each must come out the same. Two spellings of one thing must not be
    two behaviours."""
    prose = f"ask {PERSON} about the ticket"
    modern, _ = run(chat(tools=[{"type": "function", "function": {
        "name": "lookup", "description": prose,
    }}]))
    legacy, _ = run(chat(functions=[{"name": "lookup", "description": prose}]))

    assert (
        modern["tools"][0]["function"]["description"]
        == legacy["functions"][0]["description"]
        == placeholder_for(prose)
    )


def test_a_person_named_in_metadata_is_redacted():
    """`metadata` is not read by the model, but it IS forwarded to the
    provider, and it fell into the structural default where a string is
    checked with the identifier rule and never rewritten. An email there
    was refused, so the position read as safe; a name went out.

    Nothing about the request depends on these values, so they are
    content. Found by asking the question Codex's P1 raised of one more
    field.
    """
    out, _ = run(chat(metadata={"ticket": "T-1", "opened_by": PERSON}))

    assert out["metadata"]["opened_by"] == placeholder_for(PERSON)
    assert out["metadata"]["ticket"] == "T-1"
    assert PERSON not in json.dumps(out)


def test_a_stop_sequence_is_checked_and_never_rewritten():
    """The other side of the same line, recorded rather than fixed.

    A stop sequence is not prose the provider reads back to anyone — it
    is matched against generated text — so redacting one would silently
    change where the model stops. It stays in the checked class: an
    email in it is refused, and a bare name is the residual, which the
    identifier rule cannot see by design.
    """
    kept, _ = run(chat(stop=["END OF ANSWER"]))
    assert kept["stop"] == ["END OF ANSWER"]

    error = refusal(chat(stop=[f"END {FIXTURE}"]))
    assert error.code == "pii_in_identifier"


MAXIMAL_CHAT: dict = {
    "model": "librerun/think",
    "user": "caller",
    "messages": [
        {"role": "system", "content": "system prompt"},
        {
            "role": "user",
            "name": "participant",
            "content": [
                {"type": "text", "text": "a text part"},
            ],
        },
        {"role": "user", "content": "a plain string content"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "send_mail",
                        "arguments": json.dumps(
                            {
                                "to": "recipient",
                                "count": 3,
                                "nested": {"inner": "deep", "n": 7},
                                "list": ["item", 11],
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "assistant",
            "function_call": {
                "name": "legacy_tool",
                "arguments": json.dumps({"field": "legacy value"}),
            },
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "tool result text"},
    ],
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "what the tool does",
                "parameters": {
                    "type": "object",
                    "title": "Parameters",
                    "description": "schema prose",
                    "required": ["q"],
                    "properties": {
                        "q": {
                            "type": "string",
                            "title": "Query",
                            "description": "field prose",
                            "pattern": "^[a-z]+$",
                            "format": "email",
                            "minLength": 1,
                            "maxLength": 40,
                            "enum": ["one", "two"],
                            "default": "one",
                            "examples": [
                                "an example",
                                {"nested_key": "nested example", "amount": 13},
                            ],
                        },
                        "n": {
                            "type": "number",
                            "const": 5,
                            "minimum": 1,
                            "maximum": 9,
                            "multipleOf": 2,
                        },
                        "sub": {
                            "type": "object",
                            "properties": {"deep": {"type": "string", "title": "Deep"}},
                        },
                    },
                    "$defs": {"Ref": {"type": "string", "title": "Ref title"}},
                },
            },
        }
    ],
    "tool_choice": {"type": "function", "function": {"name": "lookup"}},
    # The legacy spellings, still accepted by the API and therefore still
    # a way text reaches a model. `functions` is a definition list like
    # `tools` with the wrapper removed, so it carries the same prose; the
    # top-level `function_call` is a selector like `tool_choice`. Codex
    # found the first missing from BOTH the walk and this fixture, which
    # is the more interesting half: the totality test can only find a
    # position the maximal request actually has.
    "functions": [
        {
            "name": "legacy_lookup",
            "description": "what the legacy function does",
            "parameters": {
                "type": "object",
                "title": "Legacy parameters",
                "description": "legacy schema prose",
                "required": ["term"],
                "properties": {
                    "term": {
                        "type": "string",
                        "title": "Term",
                        "description": "legacy field prose",
                        "enum": ["alpha", "beta"],
                        "default": "alpha",
                        "minLength": 2,
                    }
                },
            },
        }
    ],
    "function_call": {"name": "legacy_lookup"},
    "metadata": {"ticket": "T-1", "opened_by": "a person"},
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "answer",
            "description": "response prose",
            "schema": {
                "type": "object",
                "title": "Answer",
                "properties": {"a": {"type": "string", "description": "a prose"}},
                "required": ["a"],
            },
        },
    },
    "temperature": 0.0,
    "max_tokens": 1500,
    "n": 1,
    "stream": False,
    "stop": ["END"],
}

MAXIMAL_EMBEDDING: dict = {
    "model": "librerun/kb_embed",
    "input": ["first query", "second query"],
    "encoding_format": "float",
    "dimensions": 256,
}

# Fields whose values are re-serialised JSON: a position inside one is
# addressed through the parsed document, so the walk descends into them.
_JSON_STRING_KEYS = ("arguments",)


def _walk_positions(node, path: tuple = ()):
    """Every string, number and object key in ``node``, by construction."""
    if isinstance(node, dict):
        for key in list(node):
            yield (path + (key,), "key")
            yield from _walk_positions(node[key], path + (key,))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_positions(item, path + (index,))
    elif isinstance(node, bool) or node is None:
        return
    elif isinstance(node, str):
        if path and path[-1] in _JSON_STRING_KEYS:
            try:
                parsed = json.loads(node)
            except ValueError:
                parsed = None
            if parsed is not None:
                yield from _walk_positions(parsed, path + ("<json>",))
                return
        yield (path, "string")
    elif isinstance(node, (int, float)):
        yield (path, "number")


def _inject(template: dict, path: tuple, kind: str, value):
    """A fresh copy of ``template`` with ``value`` at ``path``."""
    body = copy.deepcopy(template)
    if "<json>" in path:
        cut = path.index("<json>")
        outer, inner = path[:cut], path[cut + 1 :]
        container = body
        for step in outer[:-1]:
            container = container[step]
        parsed = json.loads(container[outer[-1]])
        _apply(parsed, inner, kind, value)
        container[outer[-1]] = json.dumps(parsed)
        return body
    _apply(body, path, kind, value)
    return body


def _apply(root, path: tuple, kind: str, value):
    node = root
    for step in path[:-1]:
        node = node[step]
    last = path[-1]
    if kind == "key":
        node[str(value)] = node.pop(last)
    else:
        node[last] = value


def _label(path: tuple) -> str:
    return "$." + ".".join(str(p) for p in path)


@pytest.mark.parametrize(
    "template,embedding,floor",
    [(MAXIMAL_CHAT, False, 60), (MAXIMAL_EMBEDDING, True, 6)],
    ids=["chat", "embeddings"],
)
def test_every_string_position_is_redacted_or_refused(template, embedding, floor):
    positions = [
        (path, kind)
        for path, kind in _walk_positions(template)
        if kind in ("string", "key")
    ]
    # A floor, not a count: the templates should GROW as the shape does,
    # and a template that quietly shrank would make this test pass by
    # probing almost nothing.
    assert len(positions) >= floor, "the maximal request stopped being maximal"

    leaked = []
    for path, kind in positions:
        body = _inject(template, path, kind, FIXTURE)
        try:
            out, _ = run(body, embedding=embedding)
        except errors.GatewayError:
            continue  # refused: nothing left the box
        if FIXTURE in json.dumps(out):
            leaked.append(f"{_label(path)} ({kind})")

    assert leaked == [], (
        "these positions reached the provider carrying the fixture, "
        f"neither redacted nor refused: {leaked}"
    )


@pytest.mark.parametrize(
    "template,embedding,floor",
    [(MAXIMAL_CHAT, False, 8), (MAXIMAL_EMBEDDING, True, 1)],
    ids=["chat", "embeddings"],
)
def test_every_number_position_is_checked_or_refused(template, embedding, floor):
    positions = [
        path for path, kind in _walk_positions(template) if kind == "number"
    ]
    assert len(positions) >= floor, "the maximal request lost its numeric positions"

    leaked = []
    for path in positions:
        body = _inject(template, path, "number", CARD_NUMBER)
        try:
            run(body, embedding=embedding)
        except errors.GatewayError:
            continue
        leaked.append(_label(path))

    assert leaked == [], (
        "these numeric positions carried a Luhn-valid card number to the "
        f"provider unrefused: {leaked}"
    )


def test_the_totality_walk_would_notice_a_hole(monkeypatch):
    """The negative test for the totality test itself.

    A green totality test is only evidence if it would go red on a real
    hole, so here is one: the walk's strict mode is replaced by a
    pass-through — precisely the "third class forwarded unexamined" the
    design forbids — and the same assertion the totality test makes must
    now find leaks. Without this, a prober that silently probed nothing
    would look exactly like a clean tree.
    """
    from gateway import redaction

    original = redaction._Walk.structure

    def forwarding(self, node, path, keys, depth):
        if isinstance(node, str):
            return node  # the hole
        return original(self, node, path, keys, depth)

    monkeypatch.setattr(redaction._Walk, "structure", forwarding)

    leaked = []
    for path, kind in _walk_positions(MAXIMAL_CHAT):
        if kind not in ("string", "key"):
            continue
        body = _inject(MAXIMAL_CHAT, path, kind, FIXTURE)
        try:
            out, _ = run(body)
        except errors.GatewayError:
            continue
        if FIXTURE in json.dumps(out):
            leaked.append(_label(path))

    assert leaked, (
        "the totality walk found no leak even with strict mode disabled — "
        "it is not probing what it claims to probe"
    )
