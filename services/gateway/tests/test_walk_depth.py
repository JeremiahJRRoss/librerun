"""Every walker enforces the depth bound, not three of ten.

`MAX_DEPTH` is advertised as the walk's resource guard, and
`content`, `structure` and `schema` enforced it. `message`,
`message_content`, `json_arguments`, `tool`, `tool_calls`,
`response_format` and `embedding_input` recursed into themselves without
ever comparing — so an embeddings `input` nested four hundred deep was
accepted although the limit is sixty-four, and one nested five thousand
deep raised `RecursionError` INSIDE the redaction walk: neither redacted
nor refused, a 500 from the one component whose job is to let nothing
through unexamined (Codex round 17, P2).

The behavioural cases below drive each path. The structural one is the
guard that matters: a per-method omission invites an eleventh method to
repeat it.
"""
from __future__ import annotations

import ast
import inspect

import pytest

from gateway import errors, redaction


def nest_list(depth: int, leaf="hello"):
    node = leaf
    for _ in range(depth):
        node = [node]
    return node


def refusal(body: dict, **kw) -> errors.GatewayError:
    with pytest.raises(errors.GatewayError) as exc:
        redaction.redact_request(body, tenant_id="t", enabled=True, **kw)
    return exc.value


def test_a_shallow_request_is_untouched_by_the_bound():
    body = {"input": nest_list(3)}
    walked, _ = redaction.redact_request(
        body, tenant_id="t", enabled=True, embedding=True
    )
    assert walked["input"] == nest_list(3)


@pytest.mark.parametrize("depth", [redaction.MAX_DEPTH + 5, 400, 5000])
def test_a_deep_embeddings_input_is_refused_not_a_recursion_error(depth):
    """5000 used to be `RecursionError`; 400 used to be accepted."""
    got = refusal({"input": nest_list(depth)}, embedding=True)
    assert got.code == "request_too_large"
    assert got.status_code == 400


def test_a_deep_tool_definition_is_refused():
    deep = {"function": None}
    node = deep
    for _ in range(5000):
        node["function"] = {"function": None}
        node = node["function"]
    got = refusal({"model": "m", "tools": [deep]})
    assert got.code == "request_too_large"


def test_a_deep_response_format_is_refused():
    deep = {"json_schema": None}
    node = deep
    for _ in range(5000):
        node["json_schema"] = {"json_schema": None}
        node = node["json_schema"]
    got = refusal({"model": "m", "response_format": deep})
    assert got.code == "request_too_large"


def deep_dict(depth: int, leaf="hi"):
    node = leaf
    for _ in range(depth):
        node = {"x": node}
    return node


def test_a_deep_message_content_is_refused():
    """A DICT, not a list: a list under `content` is a parts array, and a
    part that is neither a string nor an object is refused as
    unredactable long before depth matters. Written as a list first, and
    it passed for the wrong reason — the refusal it got was
    `binary_not_redactable`."""
    got = refusal({"messages": [{"role": "user", "content": deep_dict(5000)}]})
    assert got.code == "request_too_large"


def test_deep_tool_calls_are_refused():
    got = refusal(
        {"messages": [{"role": "assistant", "tool_calls": deep_dict(5000, {})}]}
    )
    assert got.code == "request_too_large"


def test_every_walker_that_takes_a_depth_checks_it():
    """The structural guard.

    Scoped to the code rather than to a list of method names: a method
    that accepts `depth` has, by taking it, said it participates in the
    bound. Requiring the call rather than the comparison is deliberate
    — `_descend` is the one expression of the rule, and a method that
    inlines its own comparison has forked it."""
    source = inspect.getsource(redaction)
    tree = ast.parse(source)
    walk_class = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "_Walk"
    )
    checked = []
    for fn in walk_class.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        takes_depth = any(a.arg == "depth" for a in fn.args.args)
        if not takes_depth or fn.name == "_descend":
            continue
        calls = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_descend"
        ]
        assert calls, (
            f"_Walk.{fn.name} takes `depth` and never calls _descend, so it "
            f"recurses past MAX_DEPTH until the interpreter stops it"
        )
        checked.append(fn.name)
    assert len(checked) >= 10, (
        f"expected every depth-taking walker, found only {checked}"
    )
