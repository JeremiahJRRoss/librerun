"""Keyless mode answers with a fixture, not a dataset.

`synth.instance` turned a schema's `minItems` straight into `count` and
built that many values in a synchronous list comprehension on the event
loop. A request may legitimately carry
`{"type":"array","minItems":1000000000}` — valid JSON Schema, and
keyless mode authenticates anyone holding an agent key and a run token —
so one request could hang or exhaust the whole stub gateway (Codex round
17, P2).

Depth alone does not bound this and neither does a per-array cap: nested
arrays multiply, so 256 items at each of 24 levels is 256**24 values
with every individual array "capped". The bound is on the TOTAL.

It refuses rather than truncating. A reply quietly carrying fewer items
than the schema demanded is exactly what a strict consumer rejects —
and this module already goes out of its way to keep numbers inside their
declared bounds for that reason.
"""
from __future__ import annotations

import json
import math
import time

import pytest

from gateway import errors, stub_provider, synth
from gateway.steps import ResolvedStep


def ask(schema: dict):
    return stub_provider.complete(
        {
            "model": "librerun/think",
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "x", "schema": schema},
            },
        },
        model="stub",
        scenario=None,
    )


def test_an_ordinary_schema_is_still_synthesised():
    answer = ask({"type": "array", "minItems": 3, "items": {"type": "string"}})
    import json

    assert len(json.loads(answer["choices"][0]["message"]["content"])) == 3


def test_a_huge_minitems_is_refused_rather_than_built():
    started = time.monotonic()
    with pytest.raises(errors.GatewayError) as caught:
        ask({"type": "array", "minItems": 1_000_000_000, "items": {"type": "integer"}})
    assert caught.value.code == "schema_too_large"
    assert caught.value.status_code == 400
    # The point is not to do the work and stop; it is not to start.
    assert time.monotonic() - started < 1.0


def test_nested_arrays_cannot_multiply_past_the_budget():
    """Each array here is modest. Together they are 256**3, which is why
    the bound has to be on the total rather than per array."""
    with pytest.raises(errors.GatewayError) as caught:
        ask(
            {
                "type": "array", "minItems": 256,
                "items": {
                    "type": "array", "minItems": 256,
                    "items": {
                        "type": "array", "minItems": 256,
                        "items": {"type": "string"},
                    },
                },
            }
        )
    assert caught.value.code == "schema_too_large"


def test_the_budget_is_a_total_not_a_per_call_allowance():
    """One `instance` call, one budget: a schema just under the limit is
    fine and one just over it is not."""
    ok = synth.instance(
        {"type": "array", "minItems": synth.MAX_NODES - 2, "items": {"type": "integer"}},
        seed="s",
    )
    assert len(ok) == synth.MAX_NODES - 2
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(
            {"type": "array", "minItems": synth.MAX_NODES + 1, "items": {"type": "integer"}},
            seed="s",
        )


@pytest.mark.parametrize("bogus", ["1e9", 3.5, None, True, [], {}])
def test_a_mininems_that_is_not_a_length_does_not_crash(bogus):
    """`int("1e9")` is a ValueError, which would escape the stub as a
    500 rather than a refusal. Only an int is a length — and `True` is
    an int in Python, which is why it is named here."""
    answer = ask({"type": "array", "minItems": bogus, "items": {"type": "string"}})
    assert answer["choices"]


def test_maxitems_still_bounds_a_large_minitems():
    """The pre-existing cap must survive the new one."""
    import json

    answer = ask(
        {"type": "array", "minItems": 50, "maxItems": 2, "items": {"type": "string"}}
    )
    assert len(json.loads(answer["choices"][0]["message"]["content"])) == 2


def test_a_tool_arguments_schema_is_bounded_too():
    """The other place a caller's schema reaches the synthesiser."""
    with pytest.raises(errors.GatewayError) as caught:
        stub_provider.complete(
            {
                "model": "librerun/think",
                "messages": [{"role": "user", "content": "hi"}],
                "tool_choice": {"type": "function", "function": {"name": "f"}},
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "f",
                            "parameters": {
                                "type": "array",
                                "minItems": 1_000_000_000,
                                "items": {"type": "integer"},
                            },
                        },
                    }
                ],
            },
            model="stub",
            scenario=None,
        )
    assert caught.value.code == "schema_too_large"


# --------------------------------------------------------------------------
# Strings have declared bounds too (Codex round 18, P2)
# --------------------------------------------------------------------------
#
# The string branch considered only `maxLength`, so a schema saying
# `{"type":"string","minLength":100}` got the fixed 52-character marker
# and a strict consumer rejected the reply — keyless mode failing at
# exactly the job it exists to do. The numeric branch a few lines above
# had always stayed inside `minimum`/`maximum` for that reason; this is
# the same rule, applied where I had not applied it.


def test_a_minimum_length_is_honoured():
    got = synth.instance({"type": "string", "minLength": 100}, seed="s", path="$")
    assert len(got) == 100


def test_the_marker_survives_padding():
    """Padding must not push the fixture marker out of the value: a
    report produced this way has to stay obviously a fixture."""
    got = synth.instance({"type": "string", "minLength": 400}, seed="s", path="$")
    assert got.startswith(synth.STUB_MARKER)


def test_padding_is_deterministic():
    """The module's own promise: same schema, same seed, same document."""
    schema = {"type": "string", "minLength": 200}
    assert synth.instance(schema, seed="s", path="$") == synth.instance(
        schema, seed="s", path="$"
    )


def test_a_minimum_and_a_maximum_together_are_both_respected():
    got = synth.instance(
        {"type": "string", "minLength": 100, "maxLength": 120}, seed="s", path="$"
    )
    assert 100 <= len(got) <= 120


def test_an_unsatisfiable_pair_keeps_the_upper_bound():
    """`minLength` above `maxLength` is a schema nothing can satisfy.
    The bound that cannot be exceeded is the one to keep — the same
    choice the numeric branch makes when `minimum` exceeds `maximum`."""
    got = synth.instance(
        {"type": "string", "minLength": 100, "maxLength": 50}, seed="s", path="$"
    )
    assert len(got) == 50


def test_a_huge_minimum_length_is_refused():
    """One value is enough to hurt: a gigabyte of characters is a single
    node, which the node budget would happily allow."""
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance({"type": "string", "minLength": 10**9}, seed="s", path="$")


@pytest.mark.parametrize("bogus", [True, "100", 3.5, None])
def test_a_minlength_that_is_not_a_length_is_ignored(bogus):
    got = synth.instance({"type": "string", "minLength": bogus}, seed="s", path="$")
    assert got.startswith(synth.STUB_MARKER)


@pytest.mark.parametrize("field", ["maxLength", "maxItems"])
def test_a_boolean_is_not_a_length(field):
    """`True` is an `int` in Python and `json.loads` gives `True` for
    JSON's `true`, so `{"maxLength": true}` would otherwise mean "at
    most one" — every string cut to a single character, every array to
    a single element, from a schema that is simply malformed.

    This case is here because the first version of these tests only
    covered `minLength`, where the exclusion changes nothing: `True` is
    1, and 1 is never above the marker's own length, so the padding
    branch is not reached either way. Removing the `bool` check passed
    the whole file. A guard needs the case where it makes a
    difference, not the case that was easiest to write."""
    if field == "maxLength":
        got = synth.instance({"type": "string", field: True}, seed="s", path="$")
        assert len(got) > 1, "a malformed maxLength truncated the value"
    else:
        got = synth.instance(
            {"type": "array", field: True, "minItems": 3, "items": {"type": "string"}},
            seed="s",
            path="$",
        )
        assert len(got) == 3, "a malformed maxItems overrode a real minItems"


def test_a_plain_string_schema_is_unchanged():
    plain = synth.instance({"type": "string"}, seed="s", path="$")
    assert plain.startswith(synth.STUB_MARKER)
    assert len(plain) < 100


# --------------------------------------------------------------------------
# `allOf` is not `anyOf` (Codex round 19, P2)
# --------------------------------------------------------------------------
#
# All three combinators went through one branch that took `options[0]`.
# For `oneOf`/`anyOf` that is right — they are a choice. For `allOf` the
# result must satisfy EVERY subschema, so a schema requiring `a` and `b`
# produced an object with only `a` and a strict consumer rejected a
# perfectly satisfiable schema.


def test_all_of_satisfies_every_branch():
    got = synth.instance(
        {
            "allOf": [
                {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
                {"type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ]
        },
        seed="s",
        path="$",
    )
    assert set(got) == {"a", "b"}
    assert isinstance(got["b"], int)


def test_all_of_tightens_bounds_rather_than_taking_the_first():
    """`allOf` on a bound means the tightest of them, which for a
    minimum is the largest — the opposite of first-wins."""
    got = synth.instance(
        {"allOf": [{"type": "string", "minLength": 10}, {"type": "string", "minLength": 80}]},
        seed="s",
        path="$",
    )
    assert len(got) == 80


def test_a_nested_all_of_is_flattened():
    got = synth.instance(
        {
            "allOf": [
                {"allOf": [{"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}]},
                {"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"]},
            ]
        },
        seed="s",
        path="$",
    )
    assert set(got) == {"x", "y"}


def test_the_parent_schemas_own_keys_survive_the_merge():
    """`allOf` beside `properties` is the ordinary composition shape: a
    base object extended by fragments."""
    got = synth.instance(
        {
            "type": "object",
            "properties": {"base": {"type": "string"}},
            "required": ["base"],
            "allOf": [{"properties": {"extra": {"type": "string"}}, "required": ["extra"]}],
        },
        seed="s",
        path="$",
    )
    assert set(got) == {"base", "extra"}


@pytest.mark.parametrize("combinator", ["oneOf", "anyOf"])
def test_a_choice_is_still_a_choice(combinator):
    """The fix must not turn `oneOf` into a merge: one branch is the
    whole point, and merging two mutually exclusive shapes would satisfy
    neither."""
    got = synth.instance(
        {
            combinator: [
                {"type": "object", "properties": {"p": {"type": "string"}}, "required": ["p"]},
                {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
            ]
        },
        seed="s",
        path="$",
    )
    assert set(got) == {"p"}


def test_all_of_stays_deterministic():
    schema = {
        "allOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
            {"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]},
        ]
    }
    assert synth.instance(schema, seed="s", path="$") == synth.instance(
        schema, seed="s", path="$"
    )


def test_all_of_still_answers_to_the_node_budget():
    """A merge must not become a way around the bound from round 17."""
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(
            {
                "allOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"minItems": 10**9},
                ]
            },
            seed="s",
            path="$",
        )


def test_the_budget_is_shared_across_an_all_of_boundary():
    """No single branch here is over the limit; together they are.

    Written after a negative test caught this file rather than the code:
    dropping `budget=budget` from the `allOf` recursion gives each merge
    a FRESH allowance, and the case above still failed because one
    branch alone exceeded `MAX_NODES`. A budget that resets per
    composition is not a budget — and a test using a schema that
    breaches it single-handed cannot tell the difference."""
    each = synth.MAX_NODES // 4
    schema = {
        "type": "object",
        "required": [f"p{i}" for i in range(6)],
        "properties": {
            f"p{i}": {
                "allOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"minItems": each},
                ]
            }
            for i in range(6)
        },
    }
    # Each property is a quarter of the allowance; six of them is not.
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(schema, seed="s", path="$")


# --------------------------------------------------------------------------
# Merging is recursive, and a bound is not always a count (round 20, P2)
# --------------------------------------------------------------------------
#
# Both of these are defects in round 19's own `allOf` fix.
#
# `properties` was a shallow dict union, so two branches constraining the
# SAME property kept only the last — which is the ordinary way to compose
# "a string, at least this long" with "at most that".
#
# And the bounds table used `_is_length`, which accepts only ints by
# design, for `minimum`/`maximum` too — so every fractional numeric bound
# after the first was silently dropped.


def test_two_branches_constraining_one_property_are_combined():
    got = synth.instance(
        {
            "allOf": [
                {"type": "object", "properties": {"x": {"type": "string", "minLength": 100}}, "required": ["x"]},
                {"type": "object", "properties": {"x": {"type": "string", "maxLength": 200}}, "required": ["x"]},
            ]
        },
        seed="s",
        path="$",
    )
    assert 100 <= len(got["x"]) <= 200


def test_an_unsatisfiable_property_pair_keeps_the_upper_bound():
    """The same choice the top-level string branch makes, reached through
    the merge rather than around it."""
    got = synth.instance(
        {
            "allOf": [
                {"type": "object", "properties": {"x": {"type": "string", "minLength": 300}}, "required": ["x"]},
                {"type": "object", "properties": {"x": {"type": "string", "maxLength": 150}}, "required": ["x"]},
            ]
        },
        seed="s",
        path="$",
    )
    assert len(got["x"]) == 150


def test_a_property_in_only_one_branch_survives_the_merge():
    got = synth.instance(
        {
            "allOf": [
                {"type": "object", "properties": {"only": {"type": "string"}}, "required": ["only"]},
                {"type": "object", "properties": {"other": {"type": "string"}}, "required": ["other"]},
            ]
        },
        seed="s",
        path="$",
    )
    assert set(got) == {"only", "other"}


@pytest.mark.parametrize(
    "bounds,check",
    [
        ([{"minimum": 1.5}, {"minimum": 2.5}], lambda v: v >= 2.5),
        ([{"maximum": 9.5}, {"maximum": 3.25}], lambda v: v <= 3.25),
        ([{"minimum": 3}, {"minimum": 7}], lambda v: v >= 7),
    ],
)
def test_numeric_bounds_tighten_whether_or_not_they_are_whole(bounds, check):
    """`minLength` is a count and only an int is one; `minimum` is a
    number and 1.5 is a perfectly good bound. One predicate for both
    dropped every fractional constraint after the first."""
    got = synth.instance(
        {"allOf": [{"type": "number", **b} for b in bounds]}, seed="s", path="$"
    )
    assert check(got), got


def test_a_boolean_is_not_a_numeric_bound_either():
    """`True` is an `int` and an instance of `(int, float)`, so the
    numeric predicate needs the same exclusion the length one has.

    A NEGATIVE minimum, because that is the only bound where accepting
    `True` changes the answer, and finding that out took two wrong
    tests. The first used minima 5.0 and `True`: `max(5.0, True)` is 5.0
    either way. The second added a maximum of 9.5 against `True`, which
    also cannot fail — the synthesised value comes from the *minimum*
    (0 by default), and 0 satisfies `<= 9.5` and `<= 1` alike.

    That is decision 78's finding, repeated in this same file two rounds
    after I wrote it down, and then repeated once more inside the fix
    for it. **A guard needs the case where it bites** is easy to state
    and evidently not easy to do; the injection is what settles it."""
    got = synth.instance(
        {
            "allOf": [
                {"type": "number", "minimum": -5.0},
                {"type": "number", "minimum": True},
            ]
        },
        seed="s",
        path="$",
    )
    assert got <= -5.0, f"a malformed boolean minimum displaced a real one: {got}"


# --------------------------------------------------------------------------
# One bound, two spellings (Codex round 21, P2)
# --------------------------------------------------------------------------
#
# `_TIGHTEN_NUMBER` tightens identical keys, so `allOf` branches using
# `minimum` in one and `exclusiveMinimum` in another both survived the
# merge — and the numeric synthesiser reads `minimum` first and merely
# adds one when an `exclusiveMinimum` exists anywhere, so
# `minimum: 0` beside `exclusiveMinimum: 5` produced 1.0.


@pytest.mark.parametrize(
    "branches,check,why",
    [
        ([{"minimum": 0}, {"exclusiveMinimum": 5}], lambda v: v > 5, "exclusive lower wins"),
        ([{"exclusiveMinimum": 2}, {"minimum": 9}], lambda v: v >= 9, "inclusive lower wins"),
        ([{"maximum": 100}, {"exclusiveMaximum": 3}], lambda v: v < 3, "exclusive upper wins"),
        ([{"exclusiveMinimum": 5}, {"exclusiveMinimum": 12}], lambda v: v > 12, "two exclusives"),
    ],
)
def test_the_two_spellings_of_one_bound_are_reconciled(branches, check, why):
    got = synth.instance(
        {"allOf": [{"type": "number", **b} for b in branches]}, seed="s", path="$"
    )
    assert check(got), f"{why}: got {got}"


def test_a_single_spelling_is_untouched():
    """The reconciliation must not disturb a schema that uses one."""
    assert synth.instance(
        {"allOf": [{"type": "number", "minimum": 7}]}, seed="s", path="$"
    ) >= 7
    assert synth.instance(
        {"allOf": [{"type": "number", "exclusiveMinimum": 7}]}, seed="s", path="$"
    ) > 7


# --------------------------------------------------------------------------
# Keyless embeddings answer at the width asked for (round 21, P2)
# --------------------------------------------------------------------------
#
# `dimensions` is forwarded to a real provider, and the stub ignored it
# and always emitted sixteen. A client written against `dimensions: 256`
# got the wrong shape in the demo and a different vector contract once
# credentials were enabled — and keyless `kb_search` could not match a
# Pinecone index whose model is not 16-dimensional.


def test_the_stub_answers_at_the_requested_width():
    from gateway import stub_provider

    for wanted in (256, 1536):
        got = stub_provider.embed({"input": "hi", "dimensions": wanted}, model="stub")
        assert len(got["data"][0]["embedding"]) == wanted


def test_no_dimensions_keeps_the_default():
    from gateway import stub_provider

    got = stub_provider.embed({"input": "hi"}, model="stub")
    assert len(got["data"][0]["embedding"]) == stub_provider.EMBEDDING_DIMENSIONS


def test_every_input_gets_the_same_width():
    from gateway import stub_provider

    got = stub_provider.embed(
        {"input": ["a", "b", "c"], "dimensions": 64}, model="stub"
    )
    assert [len(d["embedding"]) for d in got["data"]] == [64, 64, 64]


@pytest.mark.parametrize("bad", [0, -3, 10**9, "256", True, 1.5])
def test_a_width_no_provider_would_serve_is_refused(bad):
    """A provider refuses a `dimensions` it cannot serve; the stub
    stands in for one, so it refuses rather than quietly answering at
    some other width — which is the whole defect."""
    from gateway import errors, stub_provider

    with pytest.raises(errors.GatewayError) as caught:
        stub_provider.embed({"input": "hi", "dimensions": bad}, model="stub")
    assert caught.value.code == "provider_refused"
    assert caught.value.param == "dimensions"


def test_the_stub_stays_deterministic_at_any_width():
    from gateway import stub_provider

    first = stub_provider.embed({"input": "hi", "dimensions": 64}, model="stub")
    second = stub_provider.embed({"input": "hi", "dimensions": 64}, model="stub")
    assert first["data"][0]["embedding"] == second["data"][0]["embedding"]


# --- exclusive bounds -------------------------------------------------
# The clamp these replace only knew how to come DOWN to a bound
# (`if value > high: value = high`), so an exclusive ceiling was answered
# with the one number it forbids (Codex round 22, P2). Exclusivity is not
# a detail of a bound, it IS the bound — and the whole reason this module
# stays inside `minimum`/`maximum` is that a strict consumer of the reply
# must accept it.


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "number", "exclusiveMaximum": 0},
        {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 0.5},
        {"type": "number", "minimum": 0, "exclusiveMaximum": 0.5},
        {"type": "number", "exclusiveMinimum": -1, "maximum": 0},
        {"type": "integer", "exclusiveMaximum": 0},
        {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 5},
        {"type": "integer", "exclusiveMinimum": -3},
    ],
)
def test_a_synthesised_number_is_strictly_inside_its_exclusive_bounds(schema):
    value = synth.instance(schema, seed="seed")
    if "exclusiveMaximum" in schema:
        assert value < schema["exclusiveMaximum"], schema
    if "exclusiveMinimum" in schema:
        assert value > schema["exclusiveMinimum"], schema
    if "maximum" in schema:
        assert value <= schema["maximum"], schema
    if "minimum" in schema:
        assert value >= schema["minimum"], schema
    assert isinstance(value, int if schema["type"] == "integer" else float)


def test_both_spellings_of_one_bound_are_reconciled_outside_all_of():
    """The reconciliation used to happen at MERGE time and so covered
    `allOf` branches only: a plain schema carrying both spellings never
    passed through it, and the synthesiser read `minimum` and answered
    0, under a bound stated in the same object. It is decided at READ
    time now, which is why this case has an answer at all."""
    value = synth.instance(
        {"type": "number", "minimum": 0, "exclusiveMinimum": 5}, seed="seed"
    )
    assert value > 5


def test_a_range_no_value_can_satisfy_is_refused_not_violated():
    """No integer lies strictly between 0 and 1. Answering anyway — which
    is what the clamp did — hands the caller a document its own validator
    rejects, and blames the platform for it."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(
            {"type": "integer", "exclusiveMinimum": 0, "exclusiveMaximum": 1},
            seed="seed",
        )


def test_the_unsatisfiable_refusal_names_the_path_not_the_value():
    with pytest.raises(errors.GatewayError) as caught:
        ask(
            {
                "type": "object",
                "properties": {
                    "ratio": {
                        "type": "integer",
                        "exclusiveMinimum": 0,
                        "exclusiveMaximum": 1,
                    }
                },
                "required": ["ratio"],
            }
        )
    assert caught.value.status_code == 400
    assert caught.value.code == "schema_unsatisfiable"
    assert caught.value.param == "$.ratio"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "integer", "minimum": float("inf")},
        {"type": "number", "minimum": float("nan")},
        {"type": "number", "maximum": float("-inf")},
        # An integer past the float range IS an infinity to a `number`:
        # no float is at least 10**400.
        {"type": "number", "minimum": 10**400},
    ],
)
def test_a_non_finite_bound_is_refused_not_crashed_on(schema):
    """`json.loads` turns `1e999` into `inf` and accepts a bare `NaN`, so
    a request body can carry either. `math.floor(inf)` is an
    OverflowError — a 500 — and a returned `nan` or `inf` serialises to
    `NaN` / `Infinity`, which is not JSON: the caller is handed something
    no strict parser will read, which is the whole defect this module
    exists to avoid."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(schema, seed="seed")


def test_no_synthesised_number_is_ever_unserialisable():
    """The property behind the case list above, stated once: whatever
    comes out of a numeric schema must survive `json.dumps` and come
    back as the same value."""
    import json as _json
    import math as _math

    for schema in (
        {"type": "number"},
        {"type": "number", "minimum": -1e308, "exclusiveMaximum": 1e308},
        {"type": "integer", "minimum": 10**30},
        {"type": "number", "exclusiveMinimum": 0, "exclusiveMaximum": 1e-300},
    ):
        value = synth.instance(schema, seed="seed")
        assert _math.isfinite(value), schema
        assert _json.loads(_json.dumps(value)) == value, schema


# --- what round 22's own fix got wrong ---------------------------------
# Two of round 23's three findings were in code round 22 added, which is
# the third round running (20, 21, 22) where the fix was where the next
# defect lived.


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "number", "exclusiveMinimum": 1e20},
        {"type": "number", "exclusiveMaximum": -1e20},
        {"type": "number", "exclusiveMinimum": 1e308, "maximum": 1.7e308},
        # Small magnitudes take the readable path and must keep working.
        {"type": "number", "exclusiveMinimum": 0},
        {"type": "number", "exclusiveMaximum": 0},
    ],
)
def test_a_one_sided_exclusive_bound_is_actually_cleared(schema):
    """`bound + 1.0` rounds straight back at large magnitudes — `1e20 +
    1.0 == 1e20` — so the value returned for `exclusiveMinimum: 1e20`
    was `1e20`, the endpoint the schema forbids. Round 22 checked
    containment on the two-sided path only, so the one-sided paths
    returned their arithmetic unexamined (Codex P2)."""
    value = synth.instance(schema, seed="seed")
    if "exclusiveMinimum" in schema:
        assert value > schema["exclusiveMinimum"], schema
    if "exclusiveMaximum" in schema:
        assert value < schema["exclusiveMaximum"], schema
    if "maximum" in schema:
        assert value <= schema["maximum"], schema


def test_the_readable_answers_survive_the_nextafter_fallback():
    """Stepping a whole unit first is what keeps the ordinary cases
    legible: a fixture reading `-1.0` is worth more to an author than
    one reading `-5e-324`, and both satisfy `exclusiveMaximum: 0`."""
    assert synth.instance({"type": "number", "exclusiveMaximum": 0}, seed="s") == -1.0
    assert synth.instance({"type": "number", "exclusiveMinimum": 0}, seed="s") == 1.0


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"type": "integer", "minimum": 10**400}, 10**400),
        ({"type": "integer", "maximum": -(10**400)}, -(10**400)),
        ({"type": "integer", "exclusiveMinimum": 10**400}, 10**400 + 1),
    ],
)
def test_an_integer_bound_beyond_the_float_range_still_answers(schema, expected):
    """Python's integers are unbounded and exact, so a 400-digit
    `minimum` is a perfectly good integer bound — it answered correctly
    until round 22's finiteness check, written in terms of
    `math.isfinite`, started converting it to a float on the way and
    raising OverflowError. That is a 500 introduced by a fix, on input
    that used to work (Codex P2).

    Nothing in the integer branch converts to float now: `math.floor` of
    an int IS that int."""
    assert synth.instance(schema, seed="seed") == expected


def test_a_huge_integer_bound_reaches_the_caller_as_a_refusal_not_a_500():
    """The `number` half was never a regression — it raised OverflowError
    before round 22 too, through `float()`. Either way an uncaught
    OverflowError is a 500; this is the controlled refusal instead."""
    with pytest.raises(errors.GatewayError) as caught:
        ask(
            {
                "type": "object",
                "properties": {"ratio": {"type": "number", "minimum": 10**400}},
                "required": ["ratio"],
            }
        )
    assert caught.value.status_code == 400
    assert caught.value.code == "schema_unsatisfiable"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "number", "minimum": 5, "maximum": 3},
        {"type": "number", "minimum": 5, "exclusiveMaximum": 5},
        {"type": "number", "exclusiveMinimum": 5, "maximum": 5},
        # Adjacent representable floats: nothing lies strictly between
        # them, however satisfiable the pair looks.
        {
            "type": "number",
            "exclusiveMinimum": 1.0,
            "exclusiveMaximum": math.nextafter(1.0, math.inf),
        },
    ],
)
def test_a_float_range_no_value_can_satisfy_is_refused(schema):
    """These exist because an injection *passed*.

    Disabling the postcondition changed no test, which round 22's
    reading would have called dead code — and it is not: it is the only
    thing standing between an unsatisfiable float range and an answer of
    `5.0` for `maximum: 3`. What the passing injection actually reported
    was a missing TEST, and the two look identical from the outside.

    Telling them apart means asking what the guard is *for* and building
    that case by hand, rather than trusting the suite to already contain
    it. Round 22 read the same signal correctly about
    `_reconcile_endpoints` — which really was inert — so the signal is
    not the answer, it is the question.
    """
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(schema, seed="seed")


# --- allOf over value constraints --------------------------------------


def _two_branches(first: dict, second: dict) -> dict:
    return {
        "allOf": [
            {"type": "object", "properties": {"x": first}, "required": ["x"]},
            {"type": "object", "properties": {"x": second}, "required": ["x"]},
        ]
    }


@pytest.mark.parametrize(
    "first, second",
    [
        ({"enum": ["a", "b"]}, {"enum": ["b"]}),
        ({"enum": ["b"]}, {"enum": ["a", "b"]}),
        ({"enum": ["a", "b"]}, {"const": "b"}),
        ({"const": "b"}, {"enum": ["a", "b"]}),
        ({"enum": ["a", "b", "c"]}, {"enum": ["b", "c"]}),
    ],
)
def test_all_of_intersects_value_constraints(first, second):
    """`allOf` means every branch, and `enum` was first-wins like any
    other non-bound keyword: branches allowing `["a","b"]` and `["b"]`
    synthesised `"a"`, violating the second although `"b"` satisfies the
    whole schema (Codex P2). `const` is a one-element enum here, so the
    two spellings intersect with each other rather than past each other
    — the same lesson `minimum`/`exclusiveMinimum` taught."""
    value = synth.instance(_two_branches(first, second), seed="seed")["x"]
    for branch in (first, second):
        allowed = [branch["const"]] if "const" in branch else branch["enum"]
        assert value in allowed, (branch, value)


def test_branches_sharing_no_value_are_refused_not_violated():
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(_two_branches({"enum": ["a"]}, {"enum": ["b"]}), seed="seed")


def test_two_constants_that_disagree_are_refused():
    """The degenerate case of the same rule: `allOf` of `const: 1` and
    `const: 2` is a schema nothing satisfies, and first-wins answered
    `1` for it."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(_two_branches({"const": 1}, {"const": 2}), seed="seed")


@pytest.mark.parametrize("schema", [{"enum": ["only"]}, {"const": 7}, {"enum": [0]}])
def test_a_single_value_constraint_is_untouched(schema):
    """The intersection must not disturb the ordinary case — including
    `0` and other falsey values, which a truthiness test would drop."""
    expected = schema["const"] if "const" in schema else schema["enum"][0]
    assert synth.instance(schema, seed="seed") == expected


def test_an_empty_enum_is_a_schema_nothing_satisfies():
    with pytest.raises(synth.Unsatisfiable):
        synth.instance({"enum": []}, seed="seed")


# --- JSON Schema equality, which is not Python's -----------------------


@pytest.mark.parametrize(
    "first, second",
    [
        ({"enum": [True]}, {"enum": [1]}),
        ({"enum": [False]}, {"enum": [0]}),
        # The lookalikes nest: `[1, 2] == [True, 2]` to Python, element
        # by element, for exactly the same reason.
        ({"enum": [[1, 2]]}, {"enum": [[True, 2]]}),
        ({"enum": [{"a": 1}]}, {"enum": [{"a": True}]}),
        ({"const": True}, {"const": 1}),
    ],
)
def test_a_boolean_never_equals_a_number_when_intersecting(first, second):
    """`True == 1` in Python and `true != 1` in JSON Schema, so
    intersecting `enum: [true]` with `enum: [1]` kept `true` and
    synthesised it for a schema nothing satisfies (Codex P2).

    Sixth time the bool-is-an-int trap has bitten in this file, after
    `_is_length`, `_is_number`, and three tests that could not fail."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(_two_branches(first, second), seed="seed")


def test_a_boolean_still_intersects_with_itself():
    """The fix must not make booleans unmatchable — `True is True`."""
    assert (
        synth.instance(_two_branches({"enum": [True]}, {"enum": [True, 1]}), seed="s")[
            "x"
        ]
        is True
    )


def test_an_integer_and_a_float_are_the_same_json_number():
    """The other half of the rule. `1` and `1.0` ARE equal in JSON
    Schema, so narrowing equality must not go so far as to separate
    them."""
    value = synth.instance(_two_branches({"enum": [1]}, {"enum": [1.0]}), seed="s")["x"]
    assert value == 1


def test_a_nested_list_that_really_does_match_still_intersects():
    value = synth.instance(
        _two_branches({"enum": [[1, 2]]}, {"enum": [[1, 2], [3]]}), seed="s"
    )["x"]
    assert value == [1, 2]


# --- an enum is narrowed by its siblings -------------------------------


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"enum": [1, 2], "minimum": 2}, 2),
        ({"enum": [1, 2, 3], "exclusiveMinimum": 2}, 3),
        ({"enum": [5, 2], "maximum": 3}, 2),
        ({"enum": ["a", "bbb"], "minLength": 2}, "bbb"),
        ({"enum": ["aaaa", "bb"], "maxLength": 2}, "bb"),
        ({"enum": [[1], [1, 2, 3]], "minItems": 2}, [1, 2, 3]),
        ({"enum": [True, 5], "type": "integer"}, 5),
        ({"enum": ["x", 7], "type": "number"}, 7),
    ],
)
def test_an_enum_candidate_must_clear_its_siblings_too(schema, expected):
    """An `enum` names the permitted values and its siblings narrow them
    further. Returning `enum[0]` answered `1` for
    `{"enum": [1, 2], "minimum": 2}` — a fixture the caller's own
    validator rejects, for a schema with a perfectly good answer in it
    (Codex P2). Both spellings of the composition reach this: the merge
    produces exactly this shape, and a plain schema is written that way."""
    assert synth.instance(schema, seed="seed") == expected


def test_the_same_narrowing_applies_through_all_of():
    assert (
        synth.instance(
            {"allOf": [{"enum": [1, 2]}, {"minimum": 2}]}, seed="seed"
        )
        == 2
    )


def test_a_const_its_siblings_forbid_is_refused():
    """The degenerate case: one permitted value, ruled out by a bound
    beside it. Answering with it anyway is the same defect."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance({"const": 1, "minimum": 2}, seed="seed")


def test_no_candidate_clearing_its_siblings_is_a_refusal():
    with pytest.raises(synth.Unsatisfiable):
        synth.instance({"enum": [1, 2], "minimum": 99}, seed="seed")


@pytest.mark.parametrize(
    "schema",
    [
        {"enum": [{"a": 1}], "unevaluatedProperties": False},
        {"enum": ["keep"], "pattern": "^n.*"},
        {"enum": [[1]], "contains": {"type": "string"}},
    ],
)
def test_a_keyword_this_module_cannot_evaluate_eliminates_nothing(schema):
    """Being unable to check something is not the same as having checked
    it. A candidate is rejected only on a rule this module can actually
    evaluate — otherwise an unfamiliar keyword would quietly turn a
    satisfiable schema into a refusal, which is the failure mode of
    every over-eager validator."""
    expected = schema["enum"][0]
    assert synth.instance(schema, seed="seed") == expected


# --- a union type, and constraints inside a container ------------------


@pytest.mark.parametrize(
    "schema, expected",
    [
        ({"type": ["string", "integer"], "enum": [1]}, 1),
        ({"type": ["null", "string"], "enum": ["x"]}, "x"),
        ({"type": ["string", "integer"], "enum": ["ok"]}, "ok"),
        # The union still narrows: only the member that matches counts.
        ({"type": ["string"], "enum": [1, "s"]}, "s"),
    ],
)
def test_a_candidate_may_match_any_declared_type(schema, expected):
    """`type` may be a union, and reducing it to its first member
    refused `{"type": ["string","integer"], "enum": [1]}` — a
    satisfiable schema turned into a refusal (Codex P2).

    This is the failure `_satisfies` is written to avoid, in the other
    direction, and it is the worse one: an over-eager check breaks
    agents that work today, where a lax one merely produces a fixture
    somebody's validator rejects."""
    assert synth.instance(schema, seed="seed") == expected


@pytest.mark.parametrize(
    "schema, expected",
    [
        (
            {"type": "array", "enum": [[1], ["x"]], "items": {"type": "string"}},
            ["x"],
        ),
        (
            {
                "type": "object",
                "enum": [{"a": 1}, {"a": "s"}],
                "properties": {"a": {"type": "string"}},
            },
            {"a": "s"},
        ),
        ({"type": "object", "enum": [{"b": 1}, {"a": 1}], "required": ["a"]}, {"a": 1}),
    ],
)
def test_a_container_candidate_is_checked_inside(schema, expected):
    """A container's constraints reach inside it, so the check has to as
    well. The scalar version of this was the previous round's finding;
    stopping at the container was the same mistake one level up."""
    assert synth.instance(schema, seed="seed") == expected


def test_a_container_enum_with_no_valid_candidate_is_refused():
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(
            {"type": "array", "enum": [[1]], "items": {"type": "string"}}, seed="seed"
        )


def test_an_unknown_keyword_beside_a_container_still_eliminates_nothing():
    assert synth.instance(
        {"type": "object", "enum": [{"a": 1}], "unevaluatedProperties": False},
        seed="seed",
    ) == {"a": 1}


# --- a value too deep to reason about ----------------------------------


def _nested_value(depth: int):
    value = 1
    for _ in range(depth):
        value = [value]
    return value


def _nested_schema(depth: int) -> dict:
    schema: dict = {"type": "integer"}
    for _ in range(depth):
        schema = {"type": "array", "items": schema}
    return schema


@pytest.mark.parametrize("depth", [900, 5000])
def test_a_deeply_nested_enum_value_is_refused_not_crashed_on(depth):
    """`_same` and `_satisfies` walk VALUES out of the request body,
    which `instance`'s own depth limit never sees — so the only thing
    stopping them was Python's recursion limit, reached as an uncaught
    RecursionError at about 900 levels. That is a 500 from a body anyone
    holding an agent key can send.

    Found by measuring rather than by review. I had reasoned that
    `json.loads` would reject such a body first and wrote that down
    without checking: it parses 1200 levels without complaint, because
    it does not recurse in Python for lists. **Reasoning is not
    measuring**, and the reasoning was load-bearing for a claim that
    there was nothing to fix."""
    schema = {
        "allOf": [
            {"enum": [_nested_value(depth)]},
            {"enum": [_nested_value(depth)]},
        ]
    }
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(schema, seed="seed")


@pytest.mark.parametrize("depth", [900, 5000])
def test_a_deeply_nested_candidate_check_is_refused_too(depth):
    """The same for `_satisfies`, which recurses into containers."""
    schema = {
        "enum": [_nested_value(depth)],
        "type": "array",
        "items": _nested_schema(depth - 1),
    }
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(schema, seed="seed")


def test_the_deep_refusal_reaches_the_caller_as_a_400():
    """Not a 500. The refusal already exists for a schema asking for more
    than a fixture; a value too deep to compare is the same statement."""
    with pytest.raises(errors.GatewayError) as caught:
        ask(
            {
                "allOf": [
                    {"enum": [_nested_value(900)]},
                    {"enum": [_nested_value(900)]},
                ]
            }
        )
    assert caught.value.status_code == 400
    assert caught.value.code == "schema_too_large"


def test_an_ordinary_nesting_depth_is_untouched():
    """The bound must not cost the ordinary case: agents really do send
    schemas a few levels deep."""
    value = _nested_value(10)
    schema = {"allOf": [{"enum": [value]}, {"enum": [value]}]}
    assert synth.instance(schema, seed="seed") == value


# --- round 27: keywords the HONOURED list claimed --------------------


def test_a_required_key_with_no_property_schema_is_still_emitted():
    """`{"type":"object","required":["x"]}` is valid and satisfiable, and
    filtering `required` through `properties` answered `{}`. A required
    key with no schema is UNCONSTRAINED, not absent (Codex P2)."""
    assert set(synth.instance({"type": "object", "required": ["x"]}, seed="s")) == {"x"}


def test_required_keys_are_emitted_whether_or_not_properties_describes_them():
    got = synth.instance(
        {
            "type": "object",
            "properties": {"a": {"type": "integer"}},
            "required": ["a", "b"],
        },
        seed="s",
    )
    assert set(got) == {"a", "b"}
    assert isinstance(got["a"], int)


def test_a_default_that_the_schema_forbids_is_not_returned():
    """`default` is an ANNOTATION in JSON Schema and need not satisfy the
    schema it sits in, so returning it unchecked answered
    `{"type":"integer","default":"unknown"}` with a string (Codex P2)."""
    value = synth.instance({"type": "integer", "default": "unknown"}, seed="s")
    assert isinstance(value, int) and not isinstance(value, bool)


def test_a_default_that_fits_is_preferred():
    """The other direction: a usable default is still the best answer —
    it is what the schema's author said a sensible value looks like."""
    assert synth.instance({"type": "integer", "default": 42}, seed="s") == 42


def test_a_combinator_keeps_the_constraints_beside_it():
    """`{"type":"integer","minimum":10,"anyOf":[{"maximum":20}]}` came
    back as a marked string: the chosen branch declares no type, and the
    type it had to respect was one level up (Codex P2)."""
    value = synth.instance(
        {"type": "integer", "minimum": 10, "anyOf": [{"maximum": 20}]}, seed="s"
    )
    assert isinstance(value, int) and 10 <= value <= 20


def test_one_of_picks_a_value_satisfying_exactly_one_branch():
    """`oneOf` means exactly one, so a value clearing two is rejected
    however well it fits the first: `{"oneOf":[{"type":"integer"},
    {"minimum":0}]}` gave `0`, which satisfies both (Codex P2)."""
    schema = {"oneOf": [{"type": "integer"}, {"minimum": 0}]}
    value = synth.instance(schema, seed="s")
    matched = sum(
        1
        for option in schema["oneOf"]
        if synth._satisfies(value, option)
    )
    assert matched == 1, f"{value!r} satisfies {matched} branches"


def test_a_disjoint_one_of_still_answers_from_the_first_branch():
    """The ordinary case must not pay for the exclusivity search."""
    assert isinstance(
        synth.instance({"oneOf": [{"type": "integer"}, {"type": "string"}]}, seed="s"),
        int,
    )


# --- round 28: the cost of an intersection, and branches that work ----


def test_a_large_disjoint_intersection_does_not_stall_the_loop():
    """Comparing every value against every other was `n * m` calls to a
    recursive `_same`: two disjoint 5,000-element enums took **7.3
    seconds inside `instance()`**, synchronously, on the gateway's event
    loop, from one authenticated request — every concurrent request
    stalled behind it (Codex P1). The node budget never saw it, because
    the intersection happens during the merge, before a node is built.

    Scalars go through a hash index now, so this is `n + m`. The numbers
    that set the bound, all measured: 5,000 each side takes **0.003s**
    indexed and **7.3s** pairwise, so a 2-second bound sits about 600x
    above the fixed version and 3.6x below the broken one. Decisive in
    both directions, and it fails in seven seconds rather than the two
    minutes a 20,000-element version needed to reach the same verdict —
    a test that takes two minutes to report a regression gets run less
    often, which is its own kind of weakening.

    4,000 a side, not the 5,000 this started at. The budget is SHARED
    across the whole request now (Codex P1), so 5,000 + 5,000 spends the
    entire 10,000-unit allowance and `instance`'s own first unit tips it
    into `SchemaTooLarge` before it can reach the unsatisfiability
    verdict. That boundary is asserted directly in the test below rather
    than left to fall out of this one. 4,000 a side keeps every tooth:
    pairwise it is 16,000,000 comparisons, still seconds over this
    bound, so a regression to `_same` fails here exactly as before."""
    left = list(range(4_000))
    right = list(range(4_000, 8_000))
    started = time.monotonic()
    with pytest.raises(synth.Unsatisfiable):
        synth.instance({"allOf": [{"enum": left}, {"enum": right}]}, seed="s")
    assert time.monotonic() - started < 2.0


def test_an_intersection_at_the_cap_refuses_as_too_large_and_does_so_fast():
    """The shared allowance has an edge, and it is asserted, not implied.

    Two 5,000-member enums cost the whole 10,000-unit budget between
    them. Under the per-call budgets this replaces, each merge got its
    own 10,000 and the pair fitted; shared, it does not. Both outcomes
    are a refusal the caller sees as one 400, so the distinction that
    matters is that it is FAST — a schema at the cap must be turned away
    promptly, not ground through."""
    left = list(range(5_000))
    right = list(range(5_000, 10_000))
    started = time.monotonic()
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance({"allOf": [{"enum": left}, {"enum": right}]}, seed="s")
    assert time.monotonic() - started < 2.0


def test_a_large_overlapping_intersection_is_still_correct():
    """Speed must not cost the answer."""
    left = list(range(5_000))
    right = list(range(4_999, 9_000))
    assert synth.instance(
        {"allOf": [{"enum": left}, {"enum": right}]}, seed="s"
    ) == 4_999


def test_container_members_are_compared_under_a_budget():
    """Comparing enums of containers is bounded, because "rare in
    practice" is not a bound.

    The work this measures went from quadratic to linear when `_key`
    learned to key containers, so two disjoint 4,000-member enums no
    longer exhaust anything — they are intersected through the index and
    correctly refused as `Unsatisfiable`, which is the right answer and
    was previously hidden behind the budget running out first. The
    property under test is unchanged: the work is bounded, and a large
    enough pair still refuses rather than running. Only where that
    boundary sits moved, from about 4,000 members to about 5,000, and it
    is asserted well past wherever it is rather than pinned to a number
    that was never a constant of nature.
    """
    # Linear now, so the old size gets the RIGHT refusal rather than the
    # budget's: these two enums genuinely share no member.
    small = {"allOf": [{"enum": [[i] for i in range(4_000)]},
                       {"enum": [[i] for i in range(4_000, 8_000)]}]}
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(small, seed="s")

    # And the bound still bites, an order of magnitude out.
    left = [[i] for i in range(50_000)]
    right = [[i] for i in range(50_000, 100_000)]
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance({"allOf": [{"enum": left}, {"enum": right}]}, seed="s")

    # Overlapping enums answer at the small size and are bounded at the
    # large one, so the bound is on the WORK and not on the refusal.
    overlap = {"allOf": [{"enum": [[i] for i in range(4_000)]},
                         {"enum": [[i] for i in range(2_000, 6_000)]}]}
    assert isinstance(synth.instance(overlap, seed="s"), list)
    big = {"allOf": [{"enum": [[i] for i in range(100_000)]},
                     {"enum": [[i] for i in range(50_000, 150_000)]}]}
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(big, seed="s")


def test_the_index_keeps_json_equality():
    """A hash index must draw the same distinctions `_same` does, or the
    speed-up would quietly reintroduce the `true == 1` defect."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance({"allOf": [{"enum": [True]}, {"enum": [1]}]}, seed="s")
    assert synth.instance({"allOf": [{"enum": [1]}, {"enum": [1.0]}]}, seed="s") == 1


def test_any_of_tries_an_option_the_parent_permits():
    """A parent constraint can contradict one branch and not another.
    `{"type":"integer","anyOf":[{"type":"string"},{"minimum":5}]}` merged
    the parent over branch 0 and answered `0`, satisfying neither
    alternative, although branch 1 yields a perfectly good `5`
    (Codex P2)."""
    schema = {"type": "integer", "anyOf": [{"type": "string"}, {"minimum": 5}]}
    value = synth.instance(schema, seed="s")
    assert isinstance(value, int) and value >= 5


def test_all_of_merges_item_schemas():
    """An item schema is a schema, so two branches describing one array's
    items compose as two branches describing one property do."""
    value = synth.instance(
        {
            "allOf": [
                {"type": "array", "items": {"type": "integer"}},
                {"type": "array", "items": {"minimum": 5}},
            ]
        },
        seed="s",
    )
    assert value and all(isinstance(v, int) and v >= 5 for v in value)


@pytest.mark.parametrize(
    "first, second, check",
    [
        ({"type": "number", "minimum": 0.5}, {"type": "integer"},
         lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0.5),
        ({"type": "integer"}, {"type": "number", "minimum": 0.5},
         lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0.5),
        ({"type": ["string", "integer"]}, {"type": "integer"},
         lambda v: isinstance(v, int) and not isinstance(v, bool)),
    ],
)
def test_all_of_narrows_the_declared_type(first, second, check):
    """`integer` is a subset of `number`, so that pair narrows rather than
    colliding. First-wins answered `0.5` for a schema satisfiable by `1`
    (Codex P2)."""
    assert check(synth.instance({"allOf": [first, second]}, seed="s"))


# --- round 29: one budget for the merge, and branches that can be built


def test_the_comparison_budget_is_shared_across_the_whole_merge():
    """A budget created per call resets on every intersection, so a
    schema can keep each one under the cap while the total grows without
    limit: 500 duplicated properties carrying reversed 50-member enums
    did hundreds of thousands of comparisons for a 300 KB schema and
    never refused (Codex P1).

    This is decision 93's own sentence — *a budget guards the work it is
    wrapped around, not the work that decides what to wrap* — written
    one round earlier about the node budget missing the merge, and then
    not applied to the budget I was adding while writing it. The lesson
    was correct and I put it in a docstring instead of in the code."""
    left = {f"k{i}": {"enum": [[j] for j in range(50)]} for i in range(500)}
    right = {f"k{i}": {"enum": [[j] for j in reversed(range(50))]} for i in range(500)}
    schema = {
        "allOf": [
            {"type": "object", "properties": left},
            {"type": "object", "properties": right},
        ]
    }
    started = time.monotonic()
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(schema, seed="s")
    assert time.monotonic() - started < 2.0


def test_an_ordinary_merge_is_nowhere_near_the_budget():
    """The bound must not reach the schemas agents actually send."""
    schema = {
        "allOf": [
            {
                "type": "object",
                "properties": {f"k{i}": {"type": "string"} for i in range(40)},
                "required": [f"k{i}" for i in range(40)],
            },
            {
                "type": "object",
                "properties": {f"k{i}": {"minLength": 5} for i in range(40)},
            },
        ]
    }
    assert len(synth.instance(schema, seed="s")) == 40


def test_a_subtype_overlap_survives_an_exact_match_beside_it():
    """Guarding the `number`/`integer` overlap behind `not common` meant
    a pair with an exact match as well narrowed to that match alone, and
    `enum: [1]` became unsatisfiable although `1` is valid under both
    branches (Codex P2). Two overlaps are two overlaps."""
    schema = {
        "enum": [1],
        "allOf": [{"type": ["number", "string"]}, {"type": ["integer", "string"]}],
    }
    assert synth.instance(schema, seed="s") == 1


def test_a_combinator_inside_an_all_of_branch_is_searched_not_collapsed():
    """Taking `options[0]` during the merge threw away the search
    `instance` does for a top-level combinator, so the first option
    conflicting with a sibling branch discarded the compatible second
    (Codex P2)."""
    value = synth.instance(
        {"allOf": [{"type": "integer"}, {"anyOf": [{"type": "string"}, {"minimum": 5}]}]},
        seed="s",
    )
    assert isinstance(value, int) and value >= 5


@pytest.mark.parametrize("combinator", ["anyOf", "oneOf"])
def test_a_branch_that_cannot_be_built_is_skipped_not_fatal(combinator):
    """A contradictory OPTION is not a contradictory schema. Branch 0
    raised and the loop never reached branch 1, which yields `5`
    (Codex P2)."""
    schema = {
        "type": "integer",
        combinator: [{"type": "string", "enum": ["x"]}, {"minimum": 5}],
    }
    value = synth.instance(schema, seed="s")
    assert isinstance(value, int) and value >= 5


@pytest.mark.parametrize("combinator", ["anyOf", "oneOf"])
def test_every_branch_contradicting_is_still_a_refusal(combinator):
    """Skipping branches must not turn a genuinely impossible schema into
    a silent answer: with nothing to stand on, it refuses."""
    schema = {
        "type": "integer",
        combinator: [
            {"type": "string", "enum": ["x"]},
            {"type": "boolean", "enum": [True]},
        ],
    }
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(schema, seed="s")


def test_the_budget_covers_building_the_index_not_only_searching_it():
    """This test exists because an injection PASSED.

    Removing the `spend()` from the index build changed nothing in the
    suite, and the honest reading of that is the one round 23 arrived
    at: either the line is inert, or the case that would prove otherwise
    is missing. It was missing. Every intersection case here had a large
    `current` and a comparable `incoming`, so the `current` loop's own
    spending covered the work either way.

    A large INCOMING against a tiny `current` separates them: 400,000
    values on the right and one on the left builds a 400,000-element set
    while the left-hand loop spends exactly once. Measured without the
    index spend: answered in 0.24s, growing linearly, with no cap at
    all. **A budget has to be spent where the work happens, not where
    the loop that interests you happens** — which is decision 93's
    sentence for the third time, now inside the fix for its second."""
    schema = {"allOf": [{"enum": [1]}, {"enum": list(range(400_000))}]}
    started = time.monotonic()
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(schema, seed="s")
    assert time.monotonic() - started < 2.0


# --- One allowance for the whole request (Codex round 30, P1) ---------


def test_branch_merges_share_the_callers_budget():
    """Decision 93, for the fourth time: a budget guards the work it is
    wrapped around, not the work that decides what to wrap.

    Every combinator option used to be merged by `_merged([option],
    rest)` with NO budget argument, so each took a fresh 10,000-unit
    allowance. A 9,000-member parent enum against 2,000 disjoint
    singleton options therefore bought 2,000 x 9,000 comparisons and
    blocked for 4.4 seconds on an ~11,000-node schema — synchronously,
    on the gateway's event loop, from one authenticated request.

    The bound is deliberately far from both sides: 4.4s broken, 0.003s
    fixed."""
    schema = {
        "enum": list(range(9_000)),
        "anyOf": [{"const": 10**9 + i} for i in range(2_000)],
    }
    started = time.monotonic()
    with pytest.raises((synth.SchemaTooLarge, synth.Unsatisfiable)):
        synth.instance(schema, seed="s")
    assert time.monotonic() - started < 1.0


def test_a_shared_budget_does_not_refuse_a_schema_whose_first_option_works():
    """The other half of the same fix, and the one that could have made
    it a regression.

    Sharing the budget without making the branches LAZY would have been
    worse than the bug: the eager `[_merged([option], rest) for option
    in options]` merged every option up front, so under one shared
    allowance it would spend the lot before the search even began and
    turn a schema that answers today into `SchemaTooLarge`. An
    over-eager refusal breaks agents that work (decision 90). Options
    are merged one at a time now, and a search that succeeds on the
    first never pays for the rest.

    The shape here is deliberate, and the first version of this test had
    it wrong. `{"type":"integer","anyOf":[{"minimum":5}, …2,000 consts]}`
    looks like the same thing and proves nothing: `_allowed_values` is
    `None` for a parent with no enum, `_intersect` returns on
    `current is None` before its first `spend()`, and so 2,000 eager
    merges cost **zero** budget. Restoring the eager comprehension left
    all 167 tests green. A merge only spends when both sides bring
    values, so the parent needs an enum too — 3,000 members against ten
    500-member options is 6,000 units per merge, and merging all ten
    eagerly is 60,000 against an allowance of 10,000.

    A passing injection is a question, not an answer: either the code is
    inert or the case that would prove otherwise does not exist. It was
    the second, for the second round running."""
    schema = {
        "enum": list(range(3_000)),
        "anyOf": [{"enum": list(range(2_500, 3_000))}]
        + [{"enum": list(range(10_000 + i, 10_500 + i))} for i in range(9)],
    }
    started = time.monotonic()
    value = synth.instance(schema, seed="s")
    assert value == 2_500
    assert time.monotonic() - started < 1.0


# --- Every nested combinator, not the first (round 30, P2) ------------


def test_two_anyof_branches_in_one_allof_are_both_honoured():
    """`carried[choice] = options` kept ONE group per kind and dropped
    every later one silently.

    `allOf` over `anyOf: [integer, string]` and `anyOf: [boolean,
    string]` answered `0`: it satisfies the first group and violates the
    second, though `"a"` satisfies both. The groups distribute — a value
    satisfies every group iff it satisfies one option from each — so the
    merge is the disjunction of the merged combinations."""
    value = synth.instance(
        {
            "allOf": [
                {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                {"anyOf": [{"type": "boolean"}, {"type": "string"}]},
            ]
        },
        seed="s",
    )
    assert isinstance(value, str), f"{value!r} violates the second group"


def test_distribution_drops_the_impossible_combinations():
    """`_merged` keeps the FIRST `type` on a contradiction — the right
    forgiveness for a schema the caller wrote, the wrong one for a
    combination this module invented. Coerced, `integer` x `boolean`
    became a plain `integer`, which is how the wrong value appeared.
    Only the satisfiable combination survives."""
    merged = synth._merged(
        [
            {"anyOf": [{"type": "integer"}, {"type": "string"}]},
            {"anyOf": [{"type": "boolean"}, {"type": "string"}]},
        ],
        {},
    )
    assert merged["anyOf"] == [{"type": "string"}]


def test_groups_with_no_common_ground_refuse():
    """An empty `anyOf` would be worse than useless: `instance` skips a
    combinator with no options, so the schema would synthesise as though
    the groups were never there."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(
            {
                "allOf": [
                    {"anyOf": [{"type": "integer"}]},
                    {"anyOf": [{"type": "boolean"}]},
                ]
            },
            seed="s",
        )


def test_a_oneof_among_anyof_groups_is_carried_not_refused():
    """This test used to pin the opposite, and the schema is why.

    `{"allOf":[{"oneOf":[integer,string]},{"anyOf":[string,boolean]}]}`
    is SATISFIABLE — `"x"` is a string, which matches exactly one arm of
    the `oneOf` and one of the `anyOf` — and the module refused it. The
    refusal was written to avoid asserting an exclusivity it had not
    checked when DISTRIBUTING a `oneOf`, which is a real hazard; it then
    fired on every shape where a single `oneOf` merely sat beside an
    `anyOf`, where nothing needs distributing at all. One `oneOf` is
    carried as a sibling now, a shape this module has understood since
    decisions 102-105, and the exclusivity is checked there as always.

    The hazard the refusal existed for is still covered, by
    `test_two_one_of_groups_still_refuse`.
    """
    schema = {
        "allOf": [
            {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            {"anyOf": [{"type": "string"}, {"type": "boolean"}]},
        ]
    }
    got = synth.instance(schema, seed="s")
    assert isinstance(got, str)
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_branch_carrying_both_kinds_contributes_both():
    """The collection loop used to `break` after the first kind it
    found, so a branch declaring BOTH `oneOf` and `anyOf` had its
    `anyOf` stripped and never carried.

    That property is what this test is for, and it is now demonstrated
    DIRECTLY rather than through a refusal. Previously both being
    collected made the shape a multi-group-with-a-`oneOf` and therefore
    an `Unsatisfiable` — which proved the `anyOf` reached the merge but
    said nothing about whether it was HONOURED. It is honoured: the
    answer must be an integer (the `oneOf`) and at least five (the
    `anyOf`), and only a value respecting both can satisfy it.
    """
    schema = {"allOf": [{"oneOf": [{"type": "integer"}],
                         "anyOf": [{"minimum": 5}]}]}
    got = synth.instance(schema, seed="s")
    assert got == 5
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_single_group_is_unchanged_by_the_distribution():
    """The common case must not pay for the fix: one group is carried
    exactly as before, not routed through the cross product."""
    merged = synth._merged(
        [{"anyOf": [{"type": "string"}, {"type": "integer"}]}], {}
    )
    assert merged["anyOf"] == [{"type": "string"}, {"type": "integer"}]


# --- `_satisfies` evaluates what it can (round 31, P2) ----------------


def test_satisfies_checks_enum_and_const():
    """`_satisfies` could not see either keyword, so every enum-only
    branch looked satisfied by every value.

    The docstring's promise is that an UNFAMILIAR keyword never
    eliminates a legal value — not that a familiar one goes unchecked.
    `enum` and `const` are rules this module can evaluate, so checking
    them is the design rather than an exception to it."""
    assert synth._satisfies(1, {"enum": [1]}) is True
    assert synth._satisfies(1, {"enum": [2]}) is False
    assert synth._satisfies(1, {"const": 1}) is True
    assert synth._satisfies(1, {"const": 2}) is False
    # An empty `enum` permits nothing, which is a rule, not an absence.
    assert synth._satisfies(1, {"enum": []}) is False
    # A non-list `enum` is not something this module can evaluate, and
    # being unable to check is not the same as having checked.
    assert synth._satisfies(1, {"enum": "nonsense"}) is True


def test_satisfies_does_not_confuse_booleans_with_numbers():
    """`True == 1` in Python and `true != 1` in JSON Schema. `_same`
    does the comparing here for exactly that reason — a membership test
    written with `in` would have passed `True` for `enum: [1]`."""
    assert synth._satisfies(True, {"enum": [1]}) is False
    assert synth._satisfies(1, {"enum": [True]}) is False
    assert synth._satisfies(True, {"enum": [True]}) is True


def test_oneof_counts_enum_branches_when_choosing_an_exclusive_value():
    """`{"oneOf":[{"enum":[1]},{"enum":[2,1]}]}` answered `1`.

    `1` really does match both branches, so it is invalid under `oneOf`;
    branch 1 offers `2`, which matches exactly one. Because `_satisfies`
    ignored `enum`, every candidate counted as matching both branches,
    no candidate ever cleared the `matched == 1` test, and the fallback
    returned the first branch's value — the invalid one (Codex P2)."""
    value = synth.instance({"oneOf": [{"enum": [1]}, {"enum": [2, 1]}]}, seed="s")
    assert value == 2
    matched = sum([value in [1], value in [2, 1]])
    assert matched == 1, f"{value!r} matches {matched} branches, not exactly one"


# --- Produce a valid fixture when one exists (round 32, P2 x3) --------


def test_oneof_searches_a_branch_s_own_alternatives():
    """A branch carrying its own combinator offers several values, and
    only the first was ever considered.

    `{"oneOf":[{"anyOf":[{"enum":[1]},{"enum":[2]}]},{"enum":[1]}]}` took
    `1` from the nested `anyOf`, found it matched both outer branches,
    and fell back to it anyway — while `2`, one step further into the
    same branch, matches exactly one (Codex P2)."""
    schema = {"oneOf": [{"anyOf": [{"enum": [1]}, {"enum": [2]}]}, {"enum": [1]}]}
    value = synth.instance(schema, seed="s")
    matched = sum([value in [1, 2], value in [1]])
    assert value == 2
    assert matched == 1, f"{value!r} matches {matched} outer branches, not exactly one"


def test_the_candidate_search_is_bounded():
    """`_candidates` caps how many values a search builds and charges
    every expansion to the shared budget, so a wide nested combinator
    refuses rather than enumerating its tree."""
    wide = {"oneOf": [{"anyOf": [{"const": i} for i in range(500)]}] * 40}
    started = time.monotonic()
    try:
        synth.instance(wide, seed="s")
    except (synth.SchemaTooLarge, synth.Unsatisfiable):
        pass
    assert time.monotonic() - started < 2.0


def test_a_type_union_tries_each_member():
    """A union is a choice, and this committed to the first non-null
    member without asking whether it could satisfy the rest.

    `{"type":["string","integer"],"minLength":2,"maxLength":1}` came
    back as the one-character string `"["`. Those length keywords
    contradict each other for a string and do not apply to an integer at
    all, so `0` satisfies the whole schema (Codex P2)."""
    value = synth.instance(
        {"type": ["string", "integer"], "minLength": 2, "maxLength": 1}, seed="s"
    )
    assert isinstance(value, int) and not isinstance(value, bool)


def test_a_type_union_keeps_its_declared_order_when_the_first_member_works():
    """The search must not reorder a union that was already fine: the
    first member still wins when it satisfies the schema."""
    assert isinstance(synth.instance({"type": ["string", "integer"]}, seed="s"), str)
    assert isinstance(synth.instance({"type": ["integer", "string"]}, seed="s"), int)


def test_a_union_contradictory_under_every_member_still_answers():
    """When no member satisfies the schema the first one's value stands.
    The schema is contradictory whichever way it is read, and producing
    the coherent thing beats inventing a refusal for the caller's own
    impossible schema — the choice this module makes throughout."""
    value = synth.instance(
        {
            "type": ["string", "integer"],
            "minLength": 2, "maxLength": 1,
            "minimum": 5, "maximum": 1,
        },
        seed="s",
    )
    assert value is not None


def test_null_is_not_chosen_over_a_usable_member():
    assert synth.instance({"type": ["null", "integer"]}, seed="s") == 0
    assert synth.instance({"type": ["null"]}, seed="s") is None


def test_an_optional_property_that_cannot_be_built_is_omitted():
    """`{"type":"object","properties":{"x":{"type":"string",
    "minLength":2,"maxLength":1}}}` is satisfiable as `{}` — `x` is not
    required — but the stub emitted a one-character `x` and returned a
    document the schema rejects (Codex P2)."""
    assert synth.instance(
        {
            "type": "object",
            "properties": {"x": {"type": "string", "minLength": 2, "maxLength": 1}},
        },
        seed="s",
    ) == {}


def test_a_required_property_is_never_omitted():
    """The forgiveness stops at `required`. A contradictory subschema on
    a key the schema insists on is the caller's own contradiction, and
    dropping the key would answer with a document that is invalid for a
    different reason."""
    out = synth.instance(
        {
            "type": "object",
            "required": ["x"],
            "properties": {"x": {"type": "string", "minLength": 2, "maxLength": 1}},
        },
        seed="s",
    )
    assert "x" in out


def test_satisfiable_optional_properties_are_still_emitted():
    """The common case must not pay for the fix."""
    out = synth.instance(
        {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        },
        seed="s",
    )
    assert set(out) == {"a", "b"}


# --- The same family, one level in (round 33, P2 x5) ------------------


def test_an_enum_leaf_offers_every_permitted_value():
    """`{"oneOf":[{"enum":[1,2]},{"enum":[1]}]}` had both leaves produce
    `1`, so nothing in the search matched exactly one branch, though `2`
    does. Expanding combinators was not enough: a leaf with several
    permitted values is a choice too (Codex P2)."""
    value = synth.instance({"oneOf": [{"enum": [1, 2]}, {"enum": [1]}]}, seed="s")
    assert value == 2


def test_an_impossible_branch_does_not_abort_the_oneof_search():
    """The catch was still in the file, just no longer around the call
    that raises.

    Round 29 established that a contradictory option is not a
    contradictory schema. Moving to `_candidates` last round left
    `_branch(option)` outside the `try`, so one impossible option
    aborted the whole search and later options were never reached — a
    fixed bug coming back through a refactor (Codex P2). It escaped from
    a second place too: the exclusivity count called `_branch(other)`
    for every sibling of every candidate, unguarded and quadratic."""
    schema = {
        "oneOf": [
            {"allOf": [{"anyOf": [{"type": "integer"}]}, {"anyOf": [{"type": "string"}]}]},
            {"const": 5},
        ]
    }
    assert synth.instance(schema, seed="s") == 5


def test_a_nested_oneof_candidate_must_clear_its_own_exclusivity():
    """`_candidates` expanded a nested `oneOf` exactly like an `anyOf`.

    `{"oneOf":[{"oneOf":[{"const":1},{"const":1}]},{"const":3}]}` yielded
    `1` from the impossible inner branch, and the outer count accepted it
    because `_satisfies` does not evaluate combinators — so the answer
    matched NEITHER outer branch, while `3` matches one (Codex P2)."""
    value = synth.instance(
        {"oneOf": [{"oneOf": [{"const": 1}, {"const": 1}]}, {"const": 3}]}, seed="s"
    )
    assert value == 3


def test_null_is_a_fallback_union_member_not_an_excluded_one():
    """Filtering `null` out of the union entirely meant
    `{"type":["null","string"],"minLength":2,"maxLength":1}` returned the
    invalid one-character string, and the numeric equivalent refused
    outright — though `null` satisfies both, since length and range
    keywords do not apply to it (Codex P2)."""
    assert synth.instance(
        {"type": ["null", "string"], "minLength": 2, "maxLength": 1}, seed="s"
    ) is None
    assert synth.instance(
        {"type": ["null", "integer"], "minimum": 5, "maximum": 1}, seed="s"
    ) is None


def test_null_is_still_the_last_member_tried():
    """"Prefer a real value" is the reason `null` goes last, and it must
    not become "never try null" again — nor "always prefer null"."""
    assert isinstance(synth.instance({"type": ["null", "string"]}, seed="s"), str)
    assert synth.instance({"type": ["null"]}, seed="s") is None


def test_an_array_prefers_empty_over_an_item_its_schema_rejects():
    """With `minItems` absent zero elements are permitted, and forcing
    one made `{"type":"array","items":{"type":"string","minLength":2,
    "maxLength":1}}` answer `["["]` — rejected by its own schema, though
    `[]` satisfies it (Codex P2). The array analogue of the optional
    property, which was fixed one round earlier in the object branch
    alone."""
    assert synth.instance(
        {"type": "array", "items": {"type": "string", "minLength": 2, "maxLength": 1}},
        seed="s",
    ) == []


def test_minitems_still_wins_over_an_unsatisfiable_item():
    """The forgiveness stops where the caller insists, exactly as it does
    for a required property."""
    out = synth.instance(
        {
            "type": "array",
            "minItems": 2,
            "items": {"type": "string", "minLength": 2, "maxLength": 1},
        },
        seed="s",
    )
    assert len(out) == 2


def test_an_ordinary_array_is_unchanged():
    assert synth.instance({"type": "array", "items": {"type": "integer"}}, seed="s") == [0]


# --- The merged form builds; the original judges (round 34, P2 x2) ----


def test_anyof_does_not_accept_a_candidate_against_an_impossible_option():
    """`_satisfies` treats a combinator as automatically satisfied — it
    judges only the keywords it implements — so the acceptance test said
    yes to an option no value can satisfy.

    `{"anyOf":[{"oneOf":[{"const":1},{"const":1}]},{"const":3}]}` fell
    back to `1` from the first branch and accepted it there, though that
    inner `oneOf` is satisfiable by nothing at all, while `3` satisfies
    the schema (Codex P2). A combinator is not an unknown keyword: this
    module evaluates them everywhere else."""
    assert synth.instance(
        {"anyOf": [{"oneOf": [{"const": 1}, {"const": 1}]}, {"const": 3}]}, seed="s"
    ) == 3


def test_oneof_membership_is_judged_against_the_original_option():
    """The merged form is lossy in exactly the way membership cares about.

    `{"type":"string","minLength":2}` merged under a parent
    `{"type":"number"}` becomes `{"type":"number","minLength":2}` — the
    option's own type is gone, because `_merged` keeps the first `type`
    on a contradiction. Counting against that, `0.0` looked like an
    exclusive match for an option it contradicts, and the schema answered
    `0.0`: a value satisfying NEITHER alternative, while `1` satisfies
    one (Codex P2)."""
    value = synth.instance(
        {"type": "number", "oneOf": [{"minimum": 1}, {"type": "string", "minLength": 2}]},
        seed="s",
    )
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    assert value >= 1


def test_the_merge_really_does_lose_the_option_s_type():
    """The mechanism behind the test above, asserted directly so the two
    cannot drift apart: if `_merged` ever stops folding the parent's type
    over the branch's, the case above would start passing for a reason
    that has nothing to do with the fix."""
    assert synth._merged([{"type": "string", "minLength": 2}], {"type": "number"}) == {
        "type": "number", "minLength": 2
    }


def test_holds_evaluates_combinators_where_satisfies_does_not():
    """The two functions differ on exactly one thing, and it is the thing
    this round was about."""
    impossible = {"oneOf": [{"const": 1}, {"const": 1}]}
    assert synth._satisfies(1, impossible) is True   # judges what it implements
    assert synth._holds(1, impossible, [synth.MAX_NODES]) is False
    workable = {"anyOf": [{"const": 1}, {"const": 2}]}
    assert synth._holds(1, workable, [synth.MAX_NODES]) is True
    assert synth._holds(3, workable, [synth.MAX_NODES]) is False
    assert synth._holds(1, {"allOf": [{"minimum": 1}, {"maximum": 1}]},
                        [synth.MAX_NODES]) is True
    assert synth._holds(5, {"allOf": [{"minimum": 1}, {"maximum": 1}]},
                        [synth.MAX_NODES]) is False


def test_holds_is_charged_to_the_budget():
    """A membership test walks the option tree, so it is work, and work
    in this module is bounded."""
    deep = {"anyOf": [{"const": i} for i in range(50)]}
    budget = [10]
    with pytest.raises(synth.SchemaTooLarge):
        synth._holds(999, deep, budget)


# --- The fix has to travel with the walk (round 35, P2 x2) ------------


def test_holds_evaluates_combinators_inside_containers():
    """`_holds` gained combinators at the top and then handed the
    container recursion back to `_satisfies`, which has none — so a
    combinator one level inside an object went unevaluated exactly as it
    had at the top a round earlier.

    `{"anyOf":[{"type":"object","properties":{"x":{"oneOf":[{"const":1},
    {"const":1}]}},"required":["x"]},{"const":3}]}` accepted `{"x":1}`,
    though that inner `oneOf` is satisfiable by nothing (Codex P2)."""
    option = {
        "type": "object",
        "properties": {"x": {"oneOf": [{"const": 1}, {"const": 1}]}},
        "required": ["x"],
    }
    assert synth._holds({"x": 1}, option, [synth.MAX_NODES]) is False
    assert synth.instance({"anyOf": [option, {"const": 3}]}, seed="s") == 3


def test_holds_evaluates_combinators_inside_arrays():
    """The same walk, the other container — asserted rather than assumed,
    since fixing one of two symmetric cases is this batch's recurring
    mistake (decision 99(e))."""
    option = {"type": "array", "items": {"oneOf": [{"const": 1}, {"const": 1}]}}
    assert synth._holds([1], option, [synth.MAX_NODES]) is False
    assert synth._holds([], option, [synth.MAX_NODES]) is True


def test_a_sibling_combinator_at_the_same_level_is_enforced():
    """The loop takes `oneOf` first and returns from inside it, and
    `rest` had both combinators stripped, so a sibling `anyOf` was never
    enforced.

    `{"oneOf":[{"const":1},{"const":2}],"anyOf":[{"const":2}]}` answered
    `1`, which violates the `anyOf`, though `2` satisfies the whole
    schema (Codex P2)."""
    assert synth.instance(
        {"oneOf": [{"const": 1}, {"const": 2}], "anyOf": [{"const": 2}]}, seed="s"
    ) == 2


def test_the_sibling_check_does_not_disturb_a_lone_combinator():
    """`siblings` is everything but the combinator being searched, so
    with only one present it is just the parent constraints — the case
    that already worked must not change."""
    assert synth.instance(
        {"type": "integer", "anyOf": [{"type": "string", "enum": ["x"]}, {"minimum": 5}]},
        seed="s",
    ) == 5
    assert synth.instance({"oneOf": [{"enum": [1]}, {"enum": [2, 1]}]}, seed="s") == 2


# --- A sibling combinator generates too (round 36, P2) ----------------


def test_a_sibling_combinator_contributes_candidates():
    """Last round taught the sibling to JUDGE; this teaches it to OFFER.

    `{"oneOf":[{"type":"integer"},{"type":"string"}],"anyOf":[{"const":
    2}]}` had the arms produce `0` and a string — both correctly
    rejected by the sibling `anyOf` — and then fell back to `0`, while
    `2` satisfies the whole schema (Codex P2). A veto that removes every
    candidate and offers none is only half a constraint."""
    assert synth.instance(
        {"oneOf": [{"type": "integer"}, {"type": "string"}], "anyOf": [{"const": 2}]},
        seed="s",
    ) == 2
    # And a shape where the top-level seeds are the ONLY source, because
    # the round-58 dedup stopped the shape above from proving it. Round 49
    # gave `_candidates` a sibling mechanism of its own, and for a schema
    # whose arms `_branch` can merge the sibling into, that nested loop
    # reaches the same value one level down — so removing the top-level
    # seeds stopped failing anything, and the audit said so.
    #
    # Here every arm carries its own combinator, so merging the sibling in
    # makes a multi-group shape `_merged` refuses by name: `_branch`
    # returns nothing, the nested loop is never reached with the sibling
    # in hand, and `5` can only come from `_sibling_seeds`.
    only_seeds = {"anyOf": [{"oneOf": [{"const": 5}]}],
                  "oneOf": [{"anyOf": [{"oneOf": [{"minimum": 0}]}]}]}
    got = synth.instance(only_seeds, seed="s")
    assert got == 5, f"answered {got!r}; the top-level seeds were the source"
    assert synth._holds(got, only_seeds, [synth.MAX_NODES])


def test_the_sibling_still_judges_a_candidate_generation_let_through():
    """Adding generation stopped the round-35 test discriminating, and
    the audit said so: injecting `siblings = rest` no longer failed
    anything. That is the question this batch keeps asking, and here the
    answer was the first one — the case was missing, not the code inert.

    Generation resolves the sibling by MERGING it into each arm, and
    `_merged` is forgiving of a type contradiction: it keeps the arm's
    type. So `{"oneOf":[{"type":"string"},{"const":2}],"anyOf":[{"type":
    "integer"}]}` builds a string from the first arm — a value the
    sibling `anyOf` forbids — and that arm clears the `oneOf` exactly
    once, so without the sibling CHECK it is returned. With it, the
    string is rejected and the second arm's `2` wins, which satisfies
    everything.

    The schema the earlier test uses cannot separate them, because there
    the merge refuses outright rather than forgiving."""
    assert synth.instance(
        {"oneOf": [{"type": "string"}, {"const": 2}], "anyOf": [{"type": "integer"}]},
        seed="s",
    ) == 2


def test_the_sibling_generates_in_the_other_direction_too():
    """`anyOf` searched with a sibling `oneOf`, not only the reverse —
    one step across, which is where the last four rounds' defects have
    been hiding."""
    assert synth.instance(
        {"anyOf": [{"type": "integer"}, {"type": "string"}], "oneOf": [{"const": 2}]},
        seed="s",
    ) == 2


def test_a_lone_combinator_is_unchanged_by_the_sibling_merge():
    """With no sibling present there is nothing extra to merge, so the
    branch is exactly what it was."""
    assert synth.instance(
        {"type": "integer", "anyOf": [{"type": "string", "enum": ["x"]}, {"minimum": 5}]},
        seed="s",
    ) == 5
    assert synth.instance({"oneOf": [{"enum": [1]}, {"enum": [2, 1]}]}, seed="s") == 2


# --- The sibling adds to the search, never subtracts (round 37) -------


def test_a_nested_combinator_arm_survives_the_sibling_merge():
    """A regression of my own making, and the failure this module treats
    as the worse one.

    Round 36 merged the sibling into every arm so it could contribute
    candidates. An arm carrying its OWN combinator plus the sibling's is
    two groups, and `_merged` refuses a multi-group shape containing a
    `oneOf` by name — so `{"oneOf":[{"oneOf":[{"const":1},{"const":2}]},
    {"const":3}],"anyOf":[{"const":2}]}` went from answering `2` on the
    previous head to raising `Unsatisfiable`. **An over-eager refusal
    breaks a schema that worked** (decision 90), and here the schema it
    broke was one my own fix had been green on a round earlier.

    When the combination cannot be built the arm is built alone: the
    sibling stops contributing candidates for that arm — where it stood
    before round 36 — and still judges every one of them."""
    assert synth.instance(
        {
            "oneOf": [{"oneOf": [{"const": 1}, {"const": 2}]}, {"const": 3}],
            "anyOf": [{"const": 2}],
        },
        seed="s",
    ) == 2


def test_the_retry_does_not_smuggle_past_the_sibling():
    """The retry drops the sibling from BUILDING, not from JUDGING. A
    value the sibling forbids must still be rejected even when the arm
    could only be built without it — otherwise the fallback for one
    failure would have quietly undone the check for another."""
    value = synth.instance(
        {
            "oneOf": [{"oneOf": [{"const": 1}, {"const": 7}]}, {"const": 3}],
            "anyOf": [{"const": 7}],
        },
        seed="s",
    )
    assert value == 7


def test_an_arm_impossible_on_its_own_is_still_skipped():
    """The retry can only widen what the search considers. An arm that
    cannot be built even without the sibling raises exactly as it did,
    and the search moves on to the next one."""
    assert synth.instance(
        {
            "oneOf": [{"allOf": [{"enum": [1]}, {"enum": [2]}]}, {"const": 5}],
            "anyOf": [{"const": 5}],
        },
        seed="s",
    ) == 5


def test_the_sibling_path_shares_the_budget_too():
    """The sibling merge is a SECOND merge site, and the budget test
    above cannot see it: that schema has no sibling, so the sibling
    branch never runs and injecting into it proved nothing. The audit
    said so — `one shared budget` stopped biting the moment its anchor
    was moved onto that line. One merge site, one case."""
    schema = {
        "enum": list(range(9_000)),
        "oneOf": [{"const": 10**9 + i} for i in range(2_000)],
        "anyOf": [{"const": 7}],
    }
    started = time.monotonic()
    with pytest.raises((synth.SchemaTooLarge, synth.Unsatisfiable)):
        synth.instance(schema, seed="s")
    assert time.monotonic() - started < 1.0


# --- Two more consequences of the retry (round 38, P2 x2) -------------


def test_a_single_option_oneof_still_faces_every_check():
    """There was a fast path for `len(options) == 1` — `instance`
    straight off the branch — and it skipped the exclusivity count and
    the sibling check both.

    With the retry dropping the sibling from the BUILD for an arm that
    cannot take it, the singleton path enforced it in neither place, so
    `{"oneOf":[{"oneOf":[{"type":"integer","minimum":2},{"type":
    "integer","maximum":0}]}],"anyOf":[{"const":5}]}` answered `2`
    where only `5` works (Codex P2). A fast path that skips the checks
    is a second implementation with fewer of them."""
    assert synth.instance(
        {
            "oneOf": [
                {"oneOf": [
                    {"type": "integer", "minimum": 2},
                    {"type": "integer", "maximum": 0},
                ]}
            ],
            "anyOf": [{"const": 5}],
        },
        seed="s",
    ) == 5
    # And the ordinary single-option schemas the fast path existed for.
    assert synth.instance({"oneOf": [{"const": 9}]}, seed="s") == 9
    assert synth.instance({"anyOf": [{"const": 9}]}, seed="s") == 9


def test_the_speculative_sibling_merge_does_not_spend_the_real_budget():
    """The merge that ends in `Unsatisfiable` still does all the enum
    intersections first, and that spend came out of the shared allowance
    and was then thrown away — so the fallback repeated the same arm
    work on a depleted budget and a satisfiable schema came back
    `SchemaTooLarge`.

    Measured at 2,000 a side: answered `1` without the sibling and
    refused with it (Codex P2). The probe budget is discarded now, so
    the real one is untouched and the fallback spends exactly what it
    would have spent had the speculative merge never run."""
    big = list(range(2_000))
    schema = {
        "enum": big,
        "oneOf": [{"enum": big, "oneOf": [{"const": 1}, {"const": 2}]}],
        "anyOf": [{"const": 1}],
    }
    assert synth.instance(schema, seed="s") == 1


def test_the_sibling_seeds_are_drawn_lazily():
    """`list(_sibling_seeds())` merged every sibling option up front and
    spent the budget doing it, so an arm that would have answered
    immediately was charged for seeds nobody needed — and the case above
    went straight back to `SchemaTooLarge`. Decision 99's laziness, one
    generator further along."""
    big = list(range(2_000))
    schema = {
        "enum": big,
        "oneOf": [{"enum": big, "oneOf": [{"const": 1}, {"const": 2}]}],
        "anyOf": [{"const": i} for i in range(200)],
    }
    started = time.monotonic()
    assert synth.instance(schema, seed="s") == 1
    assert time.monotonic() - started < 2.0


def test_the_sibling_merge_is_load_bearing_on_the_anyof_path():
    """Adding the sibling SEEDS made two older audit cases stop biting,
    and the question this batch keeps asking had the same answer it had
    in rounds 29, 32 and 36: the case was missing, not the code inert.

    The seeds are wired into the `oneOf` search only, so on the `anyOf`
    path the merge is the sibling's only way in — and a seed built from
    the sibling ALONE carries none of the arm's bounds. Here the arm
    wants an integer at most 100, the sibling at least 50: the arm alone
    gives `0` (rejected), and only the merge proposes `50`."""
    assert synth.instance(
        {"anyOf": [{"type": "integer", "maximum": 100}], "oneOf": [{"minimum": 50}]},
        seed="s",
    ) == 50


def test_the_retry_is_load_bearing_when_the_seeds_cannot_answer():
    """The other case that stopped biting, for the same reason.

    A sibling whose options are a TYPE rather than a value cannot seed
    an answer — `{"type":"integer"}` seeds `0`, which matches no arm. So
    the arm has to be built alone, and its own alternatives supply `7`.
    Without the retry the arm yields nothing and the fallback answers
    `3`, which the sibling permits but the outer `oneOf` does not."""
    assert synth.instance(
        {
            "oneOf": [{"oneOf": [{"const": 7}, {"const": 8}]}, {"const": 3}],
            "anyOf": [{"type": "integer"}],
        },
        seed="s",
    ) == 7


def test_the_anyof_path_reaches_a_sibling_only_value_too():
    """Walking one step across before being told to. The `anyOf`
    analogue of the singleton case — an arm carrying its own combinator,
    so the sibling cannot merge in — already answers correctly, and this
    records that rather than assuming it."""
    assert synth.instance(
        {
            "anyOf": [{"oneOf": [
                {"type": "integer", "minimum": 2},
                {"type": "integer", "maximum": 0},
            ]}],
            "oneOf": [{"const": 5}],
        },
        seed="s",
    ) == 5


# --- A source an older one can starve is not a source (round 39) ------


def test_arm_candidates_cannot_starve_the_sibling_seeds():
    """The seeds are drawn after the arms and shared one cap with them,
    so an arm could spend the whole thing on candidates the sibling
    rejects and the only source that could answer never got to speak.

    `{"oneOf":[{"oneOf":[{"enum":[0…31,50]},{"const":0}]}],"anyOf":
    [{"const":50}]}` answered `0` — violating the inner exclusivity AND
    the sibling — while the seed `50` satisfies everything (Codex P2).
    A newly added source that an older one can starve is not a source."""
    assert synth.instance(
        {
            "oneOf": [{"oneOf": [
                {"enum": list(range(32)) + [50]},
                {"const": 0},
            ]}],
            "anyOf": [{"const": 50}],
        },
        seed="s",
    ) == 50
    # The construction above stopped separating the two allowances once
    # round 58 made a repeated candidate free: the arm was burning the cap
    # on values it had already offered, so sharing one cap no longer
    # starved anything. The burn has to be DISTINCT to cost what it used
    # to, which is what the sibling enum does here — and the answer `0`
    # has to come from the seeds, matching the `integer` arm alone while
    # every value the arms reach on their own matches both.
    shared = {
        "oneOf": [{"type": "number", "exclusiveMinimum": 0},
                  {"type": "integer", "minimum": 0}],
        "anyOf": [{"enum": list(range(synth.MAX_CANDIDATES * 2 + 1))}],
    }
    got = synth.instance(shared, seed="s")
    assert got == 0, f"answered {got!r}; the seeds shared the arms' cap"
    assert synth._holds(got, shared, [synth.MAX_NODES])


def test_an_optional_property_is_judged_through_its_combinators():
    """`_satisfies` where `_holds` belongs. Round 34 made every
    membership question combinator-aware and round 35 made it walk into
    containers; these two sites, which decide whether an optional member
    is fit to keep, were never moved over.

    `{"type":"object","properties":{"x":{"oneOf":[{"const":1},{"const":
    1}]}}}` kept `x` because `_satisfies` cannot see that inner `oneOf`,
    though `{}` satisfies the schema (Codex P2)."""
    assert synth.instance(
        {"type": "object", "properties": {"x": {"oneOf": [{"const": 1}, {"const": 1}]}}},
        seed="s",
    ) == {}


def test_an_optional_array_item_is_judged_through_its_combinators():
    """The array's half of the same pair — the THIRD time in this batch
    that the object branch and the array branch needed the identical
    change (decisions 99(e), 101(a)). Codex flagged it in passing rather
    than as its own finding; it is asserted here as its own test."""
    assert synth.instance(
        {"type": "array", "items": {"oneOf": [{"const": 1}, {"const": 1}]}},
        seed="s",
    ) == []


def test_the_combinator_aware_member_check_does_not_over_refuse():
    """Both directions, because a check that got stricter is exactly the
    kind that starts dropping members it should keep — and the two
    "the caller insisted" exceptions still hold."""
    out = synth.instance(
        {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "integer"}}},
        seed="s",
    )
    assert set(out) == {"a", "b"}
    assert synth.instance({"type": "array", "items": {"type": "integer"}}, seed="s") == [0]
    assert "x" in synth.instance(
        {
            "type": "object",
            "required": ["x"],
            "properties": {"x": {"oneOf": [{"const": 1}, {"const": 1}]}},
        },
        seed="s",
    )
    assert len(synth.instance(
        {"type": "array", "minItems": 2, "items": {"oneOf": [{"const": 1}, {"const": 1}]}},
        seed="s",
    )) == 2


def test_a_huge_enum_membership_scan_does_not_hold_the_event_loop():
    """The scan was linear, recursive and UNBILLED, once per candidate.

    `_intersect` learned this in its own round — indexed, not quadratic,
    after two disjoint 5,000-element enums took 7.3 seconds inside
    `instance()`. `_satisfies` kept the linear `_same` scan and is
    reached once per CANDIDATE, so the cost came back multiplied: 32
    candidates against a 180,000-value sibling enum spent 1.7 seconds
    and 194 nodes. The time grew with the enum; the charge did not move.
    `stub_provider.complete` is synchronous end to end and
    `_bounded_sync` cannot interrupt synchronous work, so that is the
    gateway's event loop, held by one authenticated request (Codex P1).
    """
    import time

    size = 180_000
    big = list(range(size))
    # Disjoint, so every candidate is rejected only after a FULL scan.
    # Drawn from inside the enum the first one matches and nothing is
    # measured — the construction has to force the rejecting scan.
    inner = list(range(size, size + synth.MAX_CANDIDATES))
    schema = {"oneOf": [{"oneOf": [{"enum": inner}]}], "anyOf": [{"enum": big}]}

    budget = [synth.MAX_NODES]
    started = time.perf_counter()
    try:
        synth.instance(schema, seed="s", budget=budget)
    except (synth.SchemaTooLarge, synth.Unsatisfiable):
        pass
    elapsed = time.perf_counter() - started

    # The DETERMINISTIC assertion comes first, because a bare wall-clock
    # threshold is a flake waiting for a loaded CI runner. The defect was
    # that the scan was not charged at all: the enum grew and the cost
    # recorded did not move. So require that the work was in fact
    # charged — which the linear `_same` scan never did.
    spent = synth.MAX_VALUE_WORK - synth._value_state(budget)["allowance"][0]
    assert spent >= size, (
        f"a {size}-value enum was scanned for {spent} units of value work; "
        "the scan is not being charged"
    )
    # Wall clock as a generous backstop, not the primary evidence:
    # 1.7s before the index and 0.1s after, on this machine.
    assert elapsed < 2.0, f"{elapsed:.2f}s in a synchronous synthesis"


def test_the_enum_index_is_built_once_and_reused():
    """Charging each comparison is not enough on its own.

    The first attempt at this fix charged the scan per comparison, and
    because the scan repeats per candidate a 200-value enum that answers
    correctly today began raising `SchemaTooLarge` — decision 90's
    over-eager refusal, caused by the fix meant to bound the work. The
    cost has to be REMOVED, as `_intersect` removed it.
    """
    permitted = list(range(500))
    budget = [synth.MAX_NODES]
    first = synth._enum_index(permitted, budget)
    spent_after_build = synth._value_state(budget)["allowance"][0]
    second = synth._enum_index(permitted, budget)
    assert second is not None
    # Same index object, and the second lookup charged nothing.
    assert synth._value_state(budget)["allowance"][0] == spent_after_build
    assert first[0] is second[0]


def test_value_work_is_charged_apart_from_the_node_budget():
    """Two resources, not one.

    Charging index builds to the node budget starved the search: a
    construction that answered `1200` correctly began answering `0`,
    which the schema rejects. The node budget bounds the structural
    walk; the value allowance bounds comparisons underneath it.
    """
    permitted = list(range(1_000))
    budget = [synth.MAX_NODES]
    synth._member(500, permitted, budget)
    assert budget[0] == synth.MAX_NODES, "value work must not touch the node budget"
    assert synth._value_state(budget)["allowance"][0] == synth.MAX_VALUE_WORK - 1_000


def test_a_big_enum_whose_answer_is_satisfiable_still_answers():
    """The over-refusal direction, which the first attempt broke.

    A schema carrying a large enum is not automatically a schema too
    large. Each of these answered correctly before this round and must
    keep answering.
    """
    for size in (10_000, 40_000, 90_000):
        big = list(range(size))
        schema = {
            "oneOf": [{"oneOf": [{"enum": list(range(size, size + 32))}]}],
            "anyOf": [{"enum": big}],
        }
        got = synth.instance(schema, seed="s")
        assert isinstance(got, int)


def test_an_enum_whose_answer_is_first_is_still_cheap():
    """`any()` short-circuits; an index build does not.

    A 180,000-value enum whose candidate sits at position 0 answered in
    6 nodes before this round. Building the index unconditionally made
    the BEST case worse, so the node charge has to stay where it was.
    """
    budget = [synth.MAX_NODES]
    schema = {"oneOf": [{"oneOf": [{"enum": list(range(32))}]}],
              "anyOf": [{"enum": list(range(180_000))}]}
    got = synth.instance(schema, seed="s", budget=budget)
    assert got == 0
    assert synth.MAX_NODES - budget[0] < 50, "the structural walk must stay small"


def test_a_failed_seed_merge_does_not_bankrupt_the_search():
    """`_sibling_seeds` merged against the LIVE shared budget.

    A seed merge that failed had already spent, so `SchemaTooLarge` was
    caught with the shared budget driven NEGATIVE and the generator then
    ended quietly: every later source found nothing left and the
    fallback was returned (Codex P2). `_branch` above had already
    settled the pattern — probe on a copy, commit the spend only on
    success — and this generator was left on the live budget.
    """
    for size in (4_000, 6_000):
        answer = size // 2
        schema = {
            "enum": list(range(size)),
            "oneOf": [{"oneOf": [{"type": "integer"}]}],
            # An expensive option FIRST, then the cheap one that answers.
            "anyOf": [{"enum": list(range(size, size * 3))}, {"const": answer}],
        }
        budget = [synth.MAX_NODES]
        got = synth.instance(schema, seed="s", budget=budget)
        assert budget[0] >= 0, "a speculative probe bankrupted the search"
        assert synth._holds(got, schema, [synth.MAX_NODES]), (
            f"returned {got!r}, which the schema rejects, though {answer} satisfies it"
        )


def test_the_top_level_seeds_still_reach_past_a_full_arm_allowance():
    """Re-armed after round 49, on a construction MEASURED to separate.

    Giving `_candidates` its own sibling seeding covered the cases the two
    older constructions here used, so injecting "the sibling seeds the
    search" and "sibling seeds have their own allowance" stopped failing
    anything: the guards decayed with nothing deleted and no test going red
    (decisions 111(d)/112(b)). A fuzz over 40,000 schemas found what still
    separates them, and shrinking it gives the shape below.

    Every value `0..31` matches BOTH `oneOf` arms, so the exclusivity count
    rejects all of them — exactly `MAX_CANDIDATES` candidates spent on
    values that cannot win. `32` matches the second arm alone and is in the
    sibling's enum, so it can only arrive from the seed path, and only if
    that path has an allowance the arms have not already emptied.
    """
    both = list(range(synth.MAX_CANDIDATES))          # 0..31, in both arms
    schema = {
        "anyOf": [{"anyOf": [{"enum": both + [synth.MAX_CANDIDATES]}]}],
        "oneOf": [{"anyOf": [{"enum": both}]},
                  {"anyOf": [{"type": "integer"}]}],
    }
    got = synth.instance(schema, seed="s")
    assert got == synth.MAX_CANDIDATES, (
        f"answered {got!r}; only the sibling seed path can reach "
        f"{synth.MAX_CANDIDATES} here"
    )
    assert synth._holds(got, schema, [synth.MAX_NODES])


def _seed_only_schema(size):
    """A schema whose only valid answer must come from the sibling SEED.

    The arm's first `MAX_CANDIDATES` values are all rejected by the
    sibling, and the one value they share sits past that cap, so the arm
    never offers it. Only the seed path can produce it.
    """
    good = 1_000_000
    arm = list(range(2_000_000, 2_000_000 + synth.MAX_CANDIDATES)) + [good]
    sib = [good] + list(range(size))
    return {"oneOf": [{"oneOf": [{"enum": arm}]}], "anyOf": [{"enum": sib}]}, good


def test_an_equal_enum_reached_through_a_merge_is_not_indexed_again():
    """The index cache is keyed by list identity, and a copy is a new one.

    `_allowed_values` copied the enum defensively and `_intersect`
    copied again when there was nothing to intersect against, so the
    same enum was charged once for the original schema and again for
    every derived one: with a 100,000-value sibling enum reachable only
    through the seed path, `_enum_index` ran 97 times over 3.3 million
    values, exhausted `MAX_VALUE_WORK`, and refused a schema the
    previous head answered — one comfortably inside the 200,000-node
    admission limit (Codex P2).
    """
    size = 100_000
    schema, good = _seed_only_schema(size)
    budget = [synth.MAX_NODES]
    got = synth.instance(schema, seed="s", budget=budget)
    assert got == good
    assert synth._holds(got, schema, [synth.MAX_NODES])

    # The construction that MEASURES the re-index is a different one.
    # The seed-only schema above proves the regression is gone, but
    # round 45 changed the shape of the search and it stopped taking the
    # path that derives the enum twice — so this guard went quiet while
    # still passing, and the injection audit is what noticed. A fix can
    # leave a guard standing and still take its teeth out; only a green
    # audit reveals it.
    #
    # Several arms each merging the same parent enum force the derived
    # copies directly, and separate 2x when the defensive copy is back.
    reused = list(range(50_000))
    many_arms = {"enum": reused, "oneOf": [{"minimum": i} for i in range(8)]}
    budget = [synth.MAX_NODES]
    try:
        synth.instance(many_arms, seed="s", budget=budget)
    except (synth.Unsatisfiable, synth.SchemaTooLarge):
        pass
    spent = synth.MAX_VALUE_WORK - synth._value_state(budget)["allowance"][0]
    assert spent < len(reused) * 1.5, (
        f"{spent} units of value work for a {len(reused)}-value enum across "
        "eight arms; it is being indexed more than once"
    )

    # The two copies are two defects and need two constructions. The one
    # above exercises `_allowed_values`; this one exercises `_intersect`
    # returning a fresh list when there is nothing to intersect against,
    # which needs the PARENT to carry no enum so the arms supply it.
    # Both were verified to separate 2.00x with their copy restored and
    # 1.00x without — measured, not assumed, because the first version
    # of this guard covered only one of them and the audit said so.
    arms_carry_it = {"oneOf": [{"enum": reused, "minimum": i} for i in range(8)]}
    budget = [synth.MAX_NODES]
    try:
        synth.instance(arms_carry_it, seed="s", budget=budget)
    except (synth.Unsatisfiable, synth.SchemaTooLarge):
        pass
    spent = synth.MAX_VALUE_WORK - synth._value_state(budget)["allowance"][0]
    assert spent < len(reused) * 1.5, (
        f"{spent} units of value work for a {len(reused)}-value enum the arms "
        "each carry; the intersection against nothing is copying it"
    )


def test_a_big_enum_reachable_only_through_the_seed_still_answers():
    """The regression this round fixed, across the sizes that broke.

    20k and 60k answered before and after; 100k and 150k answered
    before, and began raising `SchemaTooLarge` once the enum scan was
    charged. A schema admitted by the request walk must not be refused
    for rebuilding an index it already had.
    """
    for size in (20_000, 60_000, 100_000, 150_000):
        schema, good = _seed_only_schema(size)
        assert synth.instance(schema, seed="s") == good


def test_synthesis_never_mutates_an_enum_it_was_given():
    """The guard for the defensive copies this round removed.

    `_allowed_values` and `_intersect` now hand back the caller's list
    rather than a copy, which is only safe while nothing writes through
    it. That is a property of the whole module, not of those two
    functions, so it is pinned here: the request body belongs to the
    caller.
    """
    arm = [3, 1, 2]
    sib = [2, 9]
    arm_before, sib_before = list(arm), list(sib)
    schema = {"oneOf": [{"enum": arm}], "anyOf": [{"enum": sib}]}
    synth.instance(schema, seed="s")
    assert arm == arm_before, "the caller's enum was reordered or rewritten"
    assert sib == sib_before, "the caller's enum was reordered or rewritten"

    source = [1, 2, 3]
    merged = synth._merged([{"enum": source}], {"type": "integer"}, [synth.MAX_NODES])
    assert source == [1, 2, 3]
    assert merged.get("enum") == [1, 2, 3]


def test_untyped_conflicting_lengths_answer_with_a_non_string():
    """A string is this module's DEFAULT, not the schema's requirement.

    `minLength` and `maxLength` constrain strings and nothing else, so
    `{"minLength":2,"maxLength":1}` is perfectly satisfiable — by any
    non-string — and the clamped one-character `"["` answered a
    satisfiable schema with a value it rejects (Codex P2).
    """
    for schema in ({"minLength": 2, "maxLength": 1},
                   {"minLength": 5, "maxLength": 3}):
        got = synth.instance(schema, seed="s")
        assert not isinstance(got, str), f"answered {got!r}, still a string"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_the_non_string_fallback_respects_the_schemas_other_keywords():
    """It is a search, not a hardcoded `0`.

    A sibling keyword can rule the first alternative out, and the next
    one has to be tried rather than returned regardless — the same
    mistake as committing to a union's first member.
    """
    schema = {"minLength": 2, "maxLength": 1, "minimum": 5}
    got = synth.instance(schema, seed="s")
    assert got != 0, "returned the first alternative though `minimum` forbids it"
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_string_schema_the_default_can_satisfy_is_untouched():
    """The over-refusal direction.

    The fallback runs only where the default has ALREADY failed. Every
    schema a string can satisfy must still get one, or keyless mode
    starts answering integers to callers asking for prose.
    """
    for schema in ({}, {"minLength": 100}, {"maxLength": 5},
                   {"minLength": 2, "maxLength": 10},
                   {"minLength": 0, "maxLength": 0}):
        got = synth.instance(schema, seed="s")
        assert isinstance(got, str), f"{schema} answered {got!r}, not a string"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_declared_type_keeps_its_contradiction():
    """`{"type":"string","minLength":2,"maxLength":1}` is UNSATISFIABLE.

    No string is both at least two characters and at most one, so no
    output can be correct and the choice is pure policy — the module
    answers a caller's own contradiction with the coherent thing rather
    than a refusal, and that is deliberate and unchanged here. The
    finding this round was only about the UNTYPED case, which is
    satisfiable. The array analogue is the same, and stays the same.
    """
    text = synth.instance({"type": "string", "minLength": 2, "maxLength": 1}, seed="s")
    assert isinstance(text, str)
    array = synth.instance({"type": "array", "minItems": 2, "maxItems": 1}, seed="s")
    assert isinstance(array, list)


def test_enum_selection_sees_combinators_inside_containers():
    """The site that picks WHICH permitted value to return was left on
    `_satisfies`, which walks into `properties` and `items` but cannot
    see a combinator once it is there.

    Decision 105 settled that every membership question in this module
    is combinator-aware; this one was missed, so an enum member matching
    two inner `oneOf` branches was returned in preference to a later one
    matching exactly one (Codex P2).
    """
    obj = {
        "type": "object", "enum": [{"x": 1}, {"x": 2}],
        "properties": {"x": {"oneOf": [{"const": 1}, {"const": 1}, {"const": 2}]}},
        "required": ["x"],
    }
    assert synth.instance(obj, seed="s") == {"x": 2}
    assert synth._holds(synth.instance(obj, seed="s"), obj, [synth.MAX_NODES])

    # The array spelling, wrong in exactly the same way. Flagged here by
    # its own case for the fourth time in this batch: these two branches
    # are one idea written twice.
    arr = {
        "type": "array", "enum": [[1], [2]],
        "items": {"oneOf": [{"const": 1}, {"const": 1}, {"const": 2}]},
    }
    assert synth.instance(arr, seed="s") == [2]
    assert synth._holds(synth.instance(arr, seed="s"), arr, [synth.MAX_NODES])


def test_untyped_properties_do_not_force_an_object():
    """`properties` and `required` constrain OBJECT instances; they do
    not require the instance to be one.

    Round 42 established this for the string default. It was a special
    case rather than a rule, and the object spelling was one keyword
    family away: `{"properties":{"x":{"type":"string","enum":[1]}},
    "required":["x"]}` raised `Unsatisfiable` at `$.x` although `0`
    satisfies the whole schema (Codex P2).
    """
    schema = {"properties": {"x": {"type": "string", "enum": [1]}}, "required": ["x"]}
    got = synth.instance(schema, seed="s")
    assert not isinstance(got, dict), f"still forced an object: {got!r}"
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_an_inferred_object_that_works_is_still_an_object():
    """The over-refusal direction for the inferred-object fallback.

    It runs only where the inferred object has ALREADY failed. Every
    schema an object can satisfy must still get one, or the common case
    — an agent asking for a structured reply — starts receiving scalars.
    """
    for schema in (
        {"properties": {"a": {"type": "string"}}},
        {"properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"properties": {"a": {"properties": {"b": {"type": "integer"}},
                              "required": ["b"]}}, "required": ["a"]},
    ):
        got = synth.instance(schema, seed="s")
        assert isinstance(got, dict), f"{schema} answered {got!r}, not an object"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_declared_object_keeps_its_contradiction():
    """`"type":"object"` is a requirement, not an inference.

    Where the caller declared the type, an impossible required property
    is their own contradiction and still refuses — the fallback must not
    leak into the declared path.
    """
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(
            {"type": "object",
             "properties": {"x": {"type": "string", "enum": [1]}},
             "required": ["x"]},
            seed="s",
        )


def test_the_untyped_fallback_is_one_rule_not_two():
    """String and object reach the same helper.

    Two special cases that happen to agree are not a rule; the next
    inferred type would have needed a third. `_other_type` is the rule.
    """
    budget = [synth.MAX_NODES]
    assert synth._other_type({"minLength": 2, "maxLength": 1}, budget) == 0
    assert synth._other_type(
        {"properties": {"x": {"type": "string", "enum": [1]}}, "required": ["x"]},
        budget,
    ) == 0
    # And it reports "nothing fits" distinctly from the value `None`,
    # which is itself a legal JSON value a schema may permit.
    assert synth._other_type({"type": "null"}, budget) is None
    assert synth._other_type(
        {"type": "string", "minLength": 2, "maxLength": 1}, budget
    ) is synth._NO_ALTERNATIVE


def _starving_enum(invalid_copies):
    """An outer `oneOf` whose valid answer sits behind N invalid leaves."""
    inner = {"oneOf": [{"const": 1}, {"const": 1}, {"const": 2}]}
    members = [{"x": 1}] * invalid_copies + [{"x": 2}]
    return {
        "oneOf": [
            {"type": "object", "enum": members,
             "properties": {"x": inner}, "required": ["x"]},
            {"type": "object", "enum": [{"x": 1}],
             "properties": {"x": inner}, "required": ["x"]},
        ]
    }


def test_a_rejected_enum_leaf_does_not_spend_candidate_capacity():
    """The limit was charged per member EXAMINED, not per member yielded.

    So copies of an invalid member exhausted the allowance before a
    valid one further down was reached. The threshold sat exactly at the
    cap, which is the signature of starvation rather than of a bad
    judgement: 31 copies answered correctly and 32 did not (Codex P2).

    A candidate that is rejected is not a candidate, and must not spend
    the capacity meant for one — decision 105(a)'s starvation, in the
    one place that decides what a candidate IS.
    """
    for copies in (2, synth.MAX_CANDIDATES - 1, synth.MAX_CANDIDATES,
                   synth.MAX_CANDIDATES + 8):
        schema = _starving_enum(copies)
        got = synth.instance(schema, seed="s")
        assert got == {"x": 2}, f"{copies} invalid copies starved the valid member"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_candidate_leaves_are_judged_through_nested_combinators():
    """Round 43 moved the loop that picks the value to RETURN and left
    this one, which decides which values are OFFERED."""
    schema = _starving_enum(1)
    assert synth.instance(schema, seed="s") == {"x": 2}


def test_scanning_past_rejects_is_still_bounded():
    """Charging on yield must not make the scan unbounded.

    `_holds` charges the node budget per call, so a long enum of
    rejects runs out of allowance rather than running forever. Measured
    at 0.03s for 20,000 members.
    """
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(_starving_enum(20_000), seed="s")


def test_ordinary_enum_schemas_are_unaffected():
    """The over-refusal direction for the stricter leaf judge."""
    for schema, expected in (
        ({"enum": [1, 2, 3]}, 1),
        ({"enum": [1, 2, 3], "minimum": 2}, 2),
        ({"oneOf": [{"enum": [1, 2]}, {"enum": [3]}]}, 1),
        ({"type": "object", "enum": [{"a": 1}],
          "properties": {"a": {"type": "integer"}}}, {"a": 1}),
    ):
        assert synth.instance(schema, seed="s") == expected


def test_a_contextually_rejected_candidate_gives_its_capacity_back():
    """The leaf was satisfiable — that is why it was offered — and what
    refused it was the CONTEXT: the exclusivity count, or a sibling.

    Such a candidate has told the search nothing and must not cost it
    the slot a later value needs. 32 leaf-valid-but-context-rejected
    values starved an exclusive one behind them, so the fallback `0` was
    returned, matching BOTH branches, while `50` matches exactly one
    (Codex P2).
    """
    n = synth.MAX_CANDIDATES
    schema = {"oneOf": [{"enum": list(range(n)) + [50]},
                        {"type": "integer", "maximum": n - 1}]}
    got = synth.instance(schema, seed="s")
    assert got == 50
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_leaf_that_cannot_be_built_spends_no_capacity():
    """The enum leaf's own sibling, three lines down in the same
    function, fixed one round later than it should have been.

    A leaf that cannot be BUILT is not a candidate: contradictory
    `anyOf` arms spent the whole allowance without yielding anything and
    starved a viable `{"const":2}` behind them. The threshold sat
    exactly at the cap again — 31 arms answered and 32 did not.
    """
    n = synth.MAX_CANDIDATES
    for arms in (n - 1, n, n + 8):
        impossible = [{"type": "integer", "minimum": 2, "maximum": 1}] * arms
        schema = {"oneOf": [{"anyOf": impossible + [{"const": 2}]}, {"const": 1}]}
        got = synth.instance(schema, seed="s")
        assert got == 2, f"{arms} contradictory arms starved the viable one"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_refunds_are_themselves_bounded():
    """A search may recover from a bounded run of unusable candidates
    and may NOT sweep an unbounded one.

    Refunding without a ceiling stops being a search and becomes a
    scan: the seed generator walked an entire 90,000-member enum, spent
    the node budget, and schemas that answered before began raising
    `SchemaTooLarge`. The audit's green-baseline check caught that, not
    the over-refusal probe — which used small schemas, where the
    regression does not show.

    So the property is not "it always finds the answer" and not "it
    always refuses". It is that the work stays bounded and fast however
    long the run of rejects is.
    """
    import time

    for n in (1_000, 20_000):
        schema = {"oneOf": [{"enum": list(range(n)) + [10 ** 9]},
                            {"type": "integer", "maximum": n - 1}]}
        budget = [synth.MAX_NODES]
        started = time.perf_counter()
        try:
            synth.instance(schema, seed="s", budget=budget)
        except (synth.SchemaTooLarge, synth.Unsatisfiable):
            pass
        assert time.perf_counter() - started < 2.0
        # And the scan did not consume the whole node allowance, which
        # is what an unbounded refund did.
        assert synth.MAX_NODES - budget[0] < synth.MAX_NODES


def test_ordinary_combinator_schemas_are_unaffected():
    """The over-refusal direction for both refunds."""
    assert synth.instance({"oneOf": [{"const": 1}, {"const": 2}]}, seed="s") == 1
    assert synth.instance(
        {"type": "integer", "minimum": 10, "anyOf": [{"maximum": 20}]}, seed="s"
    ) == 10
    assert synth.instance(
        {"oneOf": [{"oneOf": [{"const": 1}, {"const": 2}]}]}, seed="s"
    ) == 1


def test_an_option_carrying_both_combinators_is_kept_not_refused():
    """A schema can hold `oneOf` and `anyOf` as SIBLINGS, and this module
    has understood that shape since decisions 102-105.

    Routing such an option through `_merged` treated the two as several
    `allOf` groups and refused, so
    `{"anyOf":[{"oneOf":[{"const":2}],"anyOf":[{"const":2}]}]}` was
    declined although `2` satisfies it — and `instance()` on the inner
    option alone answers `2` (Codex P2). A refusal is right when a value
    might violate the schema and wrong when the schema is SATISFIABLE
    and the module simply dropped a shape it could have kept.
    """
    for outer in ("oneOf", "anyOf"):
        schema = {outer: [{"oneOf": [{"const": 2}], "anyOf": [{"const": 2}]}]}
        got = synth.instance(schema, seed="s")
        assert got == 2
        assert synth._holds(got, schema, [synth.MAX_NODES])

    # Not only the redundant spelling: the two combinators may differ.
    wider = {"anyOf": [{"oneOf": [{"const": 2}],
                        "anyOf": [{"const": 2}, {"const": 3}]}]}
    assert synth.instance(wider, seed="s") == 2


def test_two_one_of_groups_still_refuse():
    """The ambiguous case the refusal was written for, unchanged.

    `oneOf` is "exactly one option matches", and there is no way to
    carry two of them: a schema has one `oneOf` key, and combining them
    would assert an exclusivity this module has not checked. One `oneOf`
    beside `anyOf` groups is a different thing entirely — it is carried,
    not combined.
    """
    for schema in (
        {"oneOf": [{"const": 1}, {"const": 2}],
         "allOf": [{"oneOf": [{"const": 1}, {"const": 3}]}]},
        {"allOf": [{"oneOf": [{"const": 1}]}, {"oneOf": [{"const": 2}]}]},
    ):
        with pytest.raises(synth.Unsatisfiable):
            synth.instance(schema, seed="s")


def test_several_any_of_groups_still_distribute():
    """The over-refusal direction for the group split."""
    schema = {"allOf": [{"anyOf": [{"type": "integer"}, {"type": "string"}]},
                        {"anyOf": [{"type": "boolean"}, {"type": "string"}]}]}
    got = synth.instance(schema, seed="s")
    assert isinstance(got, str)
    assert synth._holds(got, schema, [synth.MAX_NODES])


_IMPOSSIBLE_ONE_OF = {"oneOf": [{"const": 1}, {"const": 1}]}


def test_one_unusable_product_does_not_abort_the_distribution():
    """An unusable COMBINATION is not an unusable schema.

    The distribution already skipped a combination whose types are
    disjoint. A combination the merge itself declines — two `oneOf`
    groups meeting in one product, which decision 112 left refusing on
    purpose — was fatal instead, so the whole distribution aborted after
    a valid product had already been built (Codex P2).
    """
    group = {"anyOf": [{"const": 0}, _IMPOSSIBLE_ONE_OF]}
    for count in (2, 3):
        schema = {"allOf": [group] * count}
        got = synth.instance(schema, seed="s")
        assert got == 0, f"{count} groups aborted on an unusable product"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_distribution_with_no_usable_product_still_refuses():
    """Skipping unusable products must not turn every contradiction into
    a fixture. When nothing survives, the groups genuinely share no
    common ground and the refusal stands."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(
            {"allOf": [{"anyOf": [_IMPOSSIBLE_ONE_OF]},
                       {"anyOf": [_IMPOSSIBLE_ONE_OF]}]},
            seed="s",
        )


def test_budget_exhaustion_is_not_swallowed_by_the_product_skip():
    """`SchemaTooLarge` is a fact about the REQUEST, not about one
    combination.

    Catching it beside `Unsatisfiable` would let a schema exceed its
    allowance by attrition — every product quietly dropped until the
    groups looked empty — so it propagates.
    """
    big = list(range(8_000))
    schema = {"allOf": [{"anyOf": [{"enum": big}, {"enum": big}]},
                        {"anyOf": [{"enum": big}, {"enum": big}]}]}
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(schema, seed="s", budget=[synth.MAX_NODES])


def test_a_contradictory_property_does_not_sink_the_whole_object():
    """A property nobody can satisfy is not an object nobody can satisfy.

    Whether it sinks the object is a question about `required`, and
    `instance` already answers it — an optional property that cannot be
    built is left out, a required one takes the object down. Raising
    from the MERGE took that decision away and answered it the strict
    way every time.

    Found by walking the callers after decision 113 rather than by a
    review: a change to what a function refuses changes every caller
    that treats refusal as fatal.
    """
    schema = {"allOf": [{"properties": {"x": {"oneOf": [{"const": 1}]}}},
                        {"properties": {"x": {"oneOf": [{"const": 2}]}}}]}
    got = synth.instance(schema, seed="s")
    assert got == {}
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_an_ordinary_property_merge_is_unaffected():
    """The over-refusal direction: two branches describing the same
    property still compose, which is what the merge is for."""
    schema = {"allOf": [{"properties": {"x": {"type": "string", "minLength": 2}}},
                        {"properties": {"x": {"maxLength": 8}}}]}
    got = synth.instance(schema, seed="s")
    assert isinstance(got, dict) and 2 <= len(got["x"]) <= 8
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_contradictory_item_does_not_sink_an_array_that_may_be_empty():
    """The array sibling of the property merge, and its own comment says
    so: "an item schema is a schema, so two branches describing the same
    array's items compose exactly as two branches describing the same
    property do".

    An item nobody can satisfy is not an array nobody can satisfy —
    whether it sinks the array is a question about `minItems`, which the
    array branch already answers. Decision 113(b) fixed the property
    half by walking the callers and did not check the sibling spelling
    its own paragraph names (Codex P2).
    """
    schema = {"allOf": [{"type": "array", "items": {"oneOf": [{"const": 1}]}},
                        {"items": {"oneOf": [{"const": 2}]}}]}
    got = synth.instance(schema, seed="s")
    assert got == []
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_contradictory_item_still_sinks_an_array_that_must_have_one():
    """`minItems` is the caller insisting, and it still wins — the same
    exception the optional-member rules have always made."""
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(
            {"allOf": [{"type": "array", "minItems": 1,
                        "items": {"oneOf": [{"const": 1}]}},
                       {"items": {"oneOf": [{"const": 2}]}}]},
            seed="s",
        )


def test_an_ordinary_items_merge_is_unaffected():
    """The over-refusal direction: two branches describing the same
    array's items still compose, which is what the merge is for."""
    schema = {"allOf": [{"type": "array", "minItems": 1,
                         "items": {"type": "integer"}},
                        {"items": {"minimum": 5}}]}
    got = synth.instance(schema, seed="s")
    assert got == [5]
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_nested_sibling_combinator_supplies_candidates_not_only_vetoes():
    """`instance`'s loop learned that a sibling GENERATES; `_candidates`,
    the recursive expander, did not — so one level down the sibling could
    veto every candidate and never offer one.

    The arms of the expanded combinator were the only source of values,
    because `rest` drops both keys. `0` and a marked string came out, both
    correctly rejected by the consumer's `_holds`, and the search fell back
    to `0` — which matches neither outer arm, while `2` satisfies the whole
    schema (Codex P2). Judging was never the gap; supplying was.
    """
    schema = {"oneOf": [
        {"oneOf": [{"type": "integer"}, {"type": "string"}],
         "anyOf": [{"const": 2}]},
        {"type": "integer", "enum": ["x"]},
    ]}
    got = synth.instance(schema, seed="s")
    assert got == 2
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_the_generator_itself_offers_the_siblings_value():
    """One level below `instance`, stated against the generator directly.

    `_candidates` is where the fix lives, so the assertion that pins it
    belongs here and not only on the answer `instance` happens to return:
    a later change that made `instance` right by another route would
    leave this one red, which is the point of it.
    """
    branch = {"oneOf": [{"type": "integer"}, {"type": "string"}],
              "anyOf": [{"const": 2}]}
    offered = list(synth._candidates(
        branch, seed="s", path="$", depth=0,
        budget=[synth.MAX_NODES], limit=[synth.MAX_CANDIDATES],
        seen=(set(), []),
    ))
    assert 2 in offered


def test_a_nested_sibling_reaches_an_arm_that_carries_its_own_combinator():
    """The sibling is an ADDITION to the search, never a subtraction.

    This is the shape that rules the merge out: an arm carrying its own
    combinator plus the sibling's is two groups, and `_merged` refuses some
    multi-group shapes by name, so merging the sibling into the arm would
    turn this schema into a refusal — decision 90's rule that an over-eager
    refusal is the worse defect. Seeded from the sibling's own options
    instead, nothing has to merge and the answer is still found.
    """
    schema = {"oneOf": [
        {"oneOf": [{"oneOf": [{"const": 1}, {"const": 2}]}, {"const": 3}],
         "anyOf": [{"const": 2}]},
        {"type": "integer", "enum": ["x"]},
    ]}
    got = synth.instance(schema, seed="s")
    assert got == 2
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_nested_sibling_still_vetoes_what_it_forbids():
    """The fix must widen what the search OFFERS without widening what it
    ACCEPTS: the value the arms alone would have produced is still refused
    when the sibling forbids it."""
    schema = {"oneOf": [
        {"oneOf": [{"const": 1}, {"const": 5}], "anyOf": [{"const": 5}]},
        {"const": 9},
    ]}
    got = synth.instance(schema, seed="s")
    assert got != 1
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_seed_no_arm_can_accept_does_not_become_the_answer():
    """A seed is a value the SIBLING permits; an arm's candidate is one the
    schema asked for. Yielding seeds unfiltered replaced a refusal with a
    plausible-looking wrong value on a schema nothing satisfies.

    `_merged` returning a branch is not evidence the arm is satisfiable —
    it leaves `{"type":"integer","enum":["x"]}` exactly as it found it — so
    the filter has to be on the value, not on whether the merge succeeded.
    """
    schema = {"oneOf": [
        {"oneOf": [{"type": "integer", "enum": ["x"]},
                   {"type": "integer", "enum": ["x"]}],
         "anyOf": [{"const": 2}]},
        {"type": "integer", "enum": ["x"]},
    ]}
    with pytest.raises(synth.Unsatisfiable):
        synth.instance(schema, seed="s")


def test_a_nested_sibling_does_not_widen_what_the_search_accepts():
    """The fix widens where candidates COME FROM, never what is accepted.

    The sibling's own option is offered as a seed, so the check that
    rejects it has to be the same one that rejected the arms' values:
    `{"const":2}` seeds `2`, the arm permits only `1`, and `_holds` must
    turn both down rather than let a seed in unexamined.
    """
    schema = {"oneOf": [{"oneOf": [{"const": 1}], "anyOf": [{"const": 2}]}]}
    budget = [synth.MAX_NODES]
    assert not synth._holds(1, schema, budget)
    assert not synth._holds(2, schema, budget)


def test_nested_sibling_seeds_are_not_starved_by_the_arms():
    """The arms run first, so on a shared counter they spend the whole cap
    before the sibling gets a turn — and a value the sibling forbids is
    still a value the arms produced.

    Measured at the boundary rather than asserted in general. With one
    allowance, 64 arms answered correctly and 96 answered `0`, which
    satisfies neither outer arm; with the seeds on their own allowance both
    answer. Past 160 arms the schema exceeds the node budget and is refused
    — unchanged by this round, and a refusal is not a wrong answer.
    """
    for count in (32, 64, 96, 128):
        schema = {"oneOf": [
            {"oneOf": [{"const": i} for i in range(count)],
             "anyOf": [{"const": count - 1}]},
            {"type": "integer", "enum": ["x"]},
        ]}
        got = synth.instance(schema, seed="s")
        assert got == count - 1, f"{count} arms answered {got!r}"


def test_a_request_too_large_to_walk_is_still_refused_not_guessed():
    """The other side of the boundary above: the seeds do not buy their way
    past the node budget. This is the pre-existing refusal, pinned so a
    later widening cannot quietly turn it into a wrong answer."""
    schema = {"oneOf": [
        {"oneOf": [{"const": i} for i in range(160)], "anyOf": [{"const": 159}]},
        {"type": "integer", "enum": ["x"]},
    ]}
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(schema, seed="s")


def _rejected_seed_schema(rejected):
    """Seeds `0..rejected-1` each satisfy the sibling and match BOTH inner
    arms, so the inner exclusivity turns every one of them down; `rejected`
    matches the second arm alone and is the answer."""
    return {"oneOf": [
        {"oneOf": [{"type": "integer", "maximum": rejected - 1},
                   {"type": "integer", "minimum": 0}],
         "anyOf": [{"enum": list(range(rejected + 1))}]},
        {"type": "integer", "enum": ["x"]},
    ]}


def test_a_rejected_nested_seed_gives_its_capacity_back():
    """A seed the filter turns down has told the search nothing.

    Round 49 added the filter — a seed no arm can accept must not become
    the answer — and charged the rejected ones anyway, which is exactly the
    starvation decision 111 is about, in the code written to satisfy it.
    The boundary sat on `MAX_CANDIDATES` itself: 31 rejected seeds answered
    correctly and 32 returned the fallback `0`, which satisfies neither
    outer arm (Codex P2).
    """
    # Past `MAX_CANDIDATES * 2` as well as either side of the cap. The
    # boundary this separates moved out when round 58 stopped charging for
    # a repeat — the seeds an arm had already offered became free — so the
    # sizes that used to straddle it now sit comfortably inside, and only
    # a larger one still tells a refunded search from an unrefunded one.
    for rejected in (synth.MAX_CANDIDATES - 1, synth.MAX_CANDIDATES,
                     synth.MAX_CANDIDATES + 1, synth.MAX_CANDIDATES * 2):
        schema = _rejected_seed_schema(rejected)
        got = synth.instance(schema, seed="s")
        assert got == rejected, f"{rejected} rejected seeds answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_nested_seed_refunds_are_themselves_bounded():
    """The residual, pinned rather than papered over.

    Refunds are capped at `MAX_CANDIDATES`, so the boundary MOVES instead
    of disappearing. Round 45 measured what unbounded refunds do at the
    level above — they turn the search into a scan and break the schemas
    the cap exists to protect — so the bound is deliberate, and a schema
    past it falls back.

    WHERE that boundary sits is not a constant of nature and this test
    does not pretend it is. It sat at 64 until round 58 stopped charging
    the allowance for a candidate the search had already offered; the
    repeats this construction produces — the second arm re-synthesising
    the first arm's value, and the seed enum re-offering it — stopped
    costing capacity, and the measured boundary moved to 73. The property
    under test is unchanged: bounded refunds mean SOME size falls back,
    and the assertion is made well past whichever size that is.
    """
    schema = _rejected_seed_schema(synth.MAX_CANDIDATES * 4)
    got = synth.instance(schema, seed="s")
    assert got != synth.MAX_CANDIDATES * 4, (
        "refunds are supposed to be bounded; an unbounded refund makes the "
        "search a scan (decision 111, measured in round 45)"
    )


def test_a_numeric_leaf_offers_a_non_integral_candidate():
    """`1.0` is an integer in JSON Schema, and `_is_type` has always said
    so — that is the whole finding.

    For `{"oneOf":[{"type":"number"},{"type":"integer"}]}` both leaves
    produce an integral value, every integral value matches BOTH arms, and
    the exclusivity count correctly rejects each one. The search was right
    and had nothing left to consider, so the fallback returned `0.0`, which
    the schema rejects, though `0.5` satisfies exactly the `number` arm
    (Codex P2). Exclusivity can turn on the integer/number boundary and
    nothing `instance` returns ever sits on the non-integral side of it.
    """
    for schema in (
        {"oneOf": [{"type": "number"}, {"type": "integer"}]},
        {"oneOf": [{"type": "integer"}, {"type": "number"}]},
        {"oneOf": [{"type": "number", "minimum": 0}, {"type": "integer"}]},
        {"oneOf": [{"oneOf": [{"type": "number"}, {"type": "integer"}]},
                   {"type": "integer", "enum": ["x"]}]},
    ):
        got = synth.instance(schema, seed="s")
        assert synth._holds(got, schema, [synth.MAX_NODES]), (
            f"{schema} answered {got!r}, which it rejects"
        )
        assert isinstance(got, float) and not float(got).is_integer()


def test_an_integral_answer_is_kept_where_it_is_correct():
    """The over-refusal direction. A leaf with no competing subtype, or
    with bounds that admit only integral values, must be untouched: the
    extra candidate is an addition to the search, never a substitution."""
    for schema, expected in (
        ({"type": "integer"}, 0),
        ({"type": "number"}, 0.0),
        ({"oneOf": [{"type": "number"}]}, 0.0),
        ({"oneOf": [{"type": "integer"}, {"type": "string"}]}, 0),
        ({"type": "number", "minimum": 3, "maximum": 3}, 3.0),
    ):
        got = synth.instance(schema, seed="s")
        assert got == expected, f"{schema} answered {got!r}, wanted {expected!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_the_non_integral_candidate_does_not_narrow_the_search():
    """Decision 116 applied to the fix that decision 116's round produced.

    Charged to `limit`, a numeric leaf spends two slots instead of one, so
    a `oneOf` of crowded numeric arms reaches half as far: schemas with 32
    to 63 such arms went from answering correctly to answering wrongly —
    the boundary landing on `MAX_CANDIDATES` and its refunds exactly. An
    over-eager change that breaks what works today is the worse defect
    (decision 90), and this one came out of a fix for the opposite
    problem. The non-integral candidates have an allowance of their own.
    """
    for count in (16, 32, 48):
        schema = {"oneOf": [{"type": "number", "minimum": 0, "maximum": 10}
                            for _ in range(count)] + [{"const": "z"}]}
        got = synth.instance(schema, seed="s")
        assert got == "z", f"{count} crowded numeric arms answered {got!r}"


def test_the_neighbour_allowance_is_bounded_across_the_search():
    """Bounded across the whole request rather than per leaf, so the extra
    breadth cannot grow with the arm count. The allowance hangs off the
    budget, which is the one object shared down the call tree."""
    budget = [synth.MAX_NODES]
    schema = {"oneOf": [{"type": "number"}, {"type": "integer"}]}
    synth.instance(schema, seed="s", budget=budget)
    spent = synth._value_state(budget).get("neighbour")
    assert spent is not None and spent[0] < synth.MAX_CANDIDATES


def _rejected_fraction_schema(n):
    """Arm A is an `anyOf` of `n` numeric ranges `[i, i+0.5]`; arm B is an
    enum of every integral value plus the first `n-1` half-values. Every
    candidate up to `(n-2)+0.5` matches BOTH arms and is rejected by the
    exclusivity count; only `(n-1)+0.5` matches arm A alone."""
    ranges = [{"type": "number", "minimum": i, "maximum": i + 0.5}
              for i in range(n)]
    shared = [float(i) for i in range(n)] + [i + 0.5 for i in range(n - 1)]
    return {"oneOf": [{"anyOf": ranges}, {"enum": shared}]}, (n - 1) + 0.5


def test_a_context_rejected_fraction_gives_its_capacity_back():
    """The fractional allowance is spent on YIELD, which is before the outer
    combinator judges the value.

    `_holds` at the leaf asks whether the fraction satisfies that leaf; the
    consumer asks whether it satisfies the outer `oneOf`. Locally valid
    fractions rejected for matching another arm emptied the allowance, and
    the one exclusive fraction behind them was never offered — the boundary
    on `MAX_CANDIDATES` for the third round running (Codex P2).

    `limit` has had bounded refunds since round 39 and `seed_limit` since
    round 50. This counter had none: three counters, two answers.
    """
    for n in (synth.MAX_CANDIDATES, synth.MAX_CANDIDATES + 1,
              synth.MAX_CANDIDATES + 8):
        schema, answer = _rejected_fraction_schema(n)
        got = synth.instance(schema, seed="s")
        assert got == answer, f"{n - 1} rejected fractions answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_neighbour_refunds_are_themselves_bounded():
    """Bounded, like the other two. The boundary moves from 32 to 64 rather
    than disappearing — round 45 measured what unbounded refunds do, and the
    answer has not changed for being asked about a third counter.

    Past the boundary the search does not reach the answer. WHICH way it
    fails is not the point and is not pinned: it returned the fallback
    before the round-55 walk and refuses with `SchemaTooLarge` after, the
    walk's extra checks having spent the node budget first. Both are the
    same statement — the refunds ran out — and decision 108 prefers the
    refusal of the two. Pinning the fallback would be pinning the worse
    outcome to keep a number steady (decision 112(b)).
    """
    schema, answer = _rejected_fraction_schema(synth.MAX_CANDIDATES * 2 + 8)
    try:
        got = synth.instance(schema, seed="s")
    except synth.SchemaTooLarge:
        return
    assert got != answer, (
        "refunds are supposed to be bounded; an unbounded refund makes the "
        "search a scan (decision 111, measured in round 45)"
    )


def test_every_candidate_counter_has_bounded_refunds():
    """The check decision 109 asks for, applied to counters rather than to
    shapes — and stated as a census so it cannot quietly go stale.

    Three counters decide how many candidates a search may consider, and
    each one spends before the consumer's verdict is known. A counter added
    later without refunds is the defect this batch found three times.
    """
    import pathlib
    import re
    source = pathlib.Path(synth.__file__).read_text()
    # Every candidate allowance in this module is a one-element list bound
    # to `MAX_CANDIDATES`. Asserting on the DECREMENT would be brittle —
    # `instance` spends through a `refundable` alias — so the census is on
    # the bound, which is the property that matters and the one a newly
    # added counter would be missing.
    bounded = set(re.findall(r"(\w+)\s*=\s*\[MAX_CANDIDATES\]", source))
    bounded |= set(re.findall(r'"(\w+)",\s*\[MAX_CANDIDATES\]', source))
    for counter, refund in (("limit", "refunds"),
                            ("seed_limit", "seed_refunds"),
                            ("neighbour", "neighbour_refunds")):
        assert counter in bounded, f"{counter} is not a bounded allowance"
        assert refund in bounded, (
            f"the {counter} allowance has no bounded refund counter; a "
            f"candidate the consumer rejects has told the search nothing, "
            f"and this is the defect the batch found three times"
        )


def test_both_fractional_offsets_are_offered():
    """`+0.5` and `-0.5` are two candidates, not one with a spare.

    Round 52 taught the leaf to refund a fraction the consumer rejected and
    then returned anyway, so the refunded capacity was thrown away on the
    same line it was handed back. For `{"oneOf":[{"type":"number"},
    {"type":"number","minimum":0}]}` both `0.0` and `+0.5` match both arms;
    the answer is `-0.5`, which was never offered (Codex P2).
    """
    negative = {"oneOf": [{"type": "number"},
                          {"type": "number", "minimum": 0}]}
    got = synth.instance(negative, seed="s")
    assert got == -0.5, f"answered {got!r}; only the negative offset is exclusive"
    assert synth._holds(got, negative, [synth.MAX_NODES])

    # The mirror, so the fix cannot be a hard-coded sign.
    positive = {"oneOf": [{"type": "number"},
                          {"type": "number", "maximum": 0}]}
    got = synth.instance(positive, seed="s")
    assert got == 0.5, f"answered {got!r}; only the positive offset is exclusive"
    assert synth._holds(got, positive, [synth.MAX_NODES])


def test_a_numeric_leaf_offers_an_integral_neighbour():
    """`±0.5` cannot satisfy an `integer` arm.

    Round 51 taught the leaf to cross the integer/number boundary and
    stopped there, so when the synthesised integer landed in an
    overlapping branch the leaf had nothing left: `{"oneOf":
    [{"type":"integer"},{"const":0}]}` answered `0`, which matches both
    arms, though `1` satisfies exactly the integer arm (Codex P2).
    Decision 109 in its third form this batch — the question is *what
    neighbours can this leaf offer*, and it was answered for one kind.
    """
    for schema, wanted in (
        ({"oneOf": [{"type": "integer"}, {"const": 0}]}, 1),
        ({"oneOf": [{"const": 0}, {"type": "integer"}]}, 1),
        ({"oneOf": [{"type": "integer"}, {"enum": [0]}]}, 1),
        # The negative direction, so the fix cannot be a hard-coded sign.
        ({"oneOf": [{"type": "integer", "maximum": 0}, {"const": 0}]}, -1),
    ):
        got = synth.instance(schema, seed="s")
        assert got == wanted, f"{schema} answered {got!r}, wanted {wanted!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_fractional_answer_still_wins_where_it_is_the_right_one():
    """The offsets are ordered, and the order is load-bearing.

    `+1` is an integer to JSON Schema even when the value is `0.0`, so
    leading with the integral neighbours would turn rounds 51 and 53 from
    `0.5` and `-0.5` into `1.0` and `-1.0`. Both are valid answers; pinning
    the ones already recorded costs nothing and churns nothing.
    """
    assert synth.instance({"oneOf": [{"type": "number"},
                                     {"type": "integer"}]}, seed="s") == 0.5
    assert synth.instance({"oneOf": [{"type": "number"},
                                     {"type": "number", "minimum": 0}]},
                          seed="s") == -0.5


def test_the_neighbour_walk_goes_past_an_occupied_step():
    """Both immediate integers can themselves match the competing branch.

    `{"oneOf":[{"type":"integer"},{"enum":[-1,0,1]}]}` has `0`, `+1` and
    `-1` all matching both arms, and `±0.5` cannot satisfy an `integer` arm
    at all, so the leaf ran out one step from the answer: `2` matches
    exactly one arm (Codex P2). The walk now steps outward under the
    neighbour allowance instead of stopping at the first ring.
    """
    for schema, wanted in (
        ({"oneOf": [{"type": "integer"}, {"enum": [-1, 0, 1]}]}, 2),
        ({"oneOf": [{"type": "integer"}, {"enum": [-2, -1, 0, 1, 2]}]}, 3),
        ({"oneOf": [{"type": "integer", "maximum": 0}, {"enum": [-1, 0]}]}, -2),
    ):
        got = synth.instance(schema, seed="s")
        assert got == wanted, f"{schema} answered {got!r}, wanted {wanted!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_the_walk_steps_past_occupied_fractions_too():
    """The sibling half of the same question, asked before it was reported.

    A competing branch can occupy `±0.5` exactly as it occupies `±1`, and
    the walk has to pass both. Decision 109 says to look for what else
    answers a question; this is that check paying off in the same round
    rather than one later.
    """
    schema = {"oneOf": [{"type": "number"},
                        {"enum": [-0.5, 0.0, 0.5, 1, -1]}]}
    got = synth.instance(schema, seed="s")
    assert got == 1.5, f"answered {got!r}"
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_the_neighbour_walk_is_bounded():
    """The walk is capped at `MAX_CANDIDATES` steps AND by the neighbour
    allowance, so a leaf whose neighbours are all refused cannot spin.

    Measured rather than asserted in the abstract: the worst case — a leaf
    pinned to a single legal value beside an arm that takes it, so every
    offset fails `_holds` and the walk runs to its cap — completes in well
    under a millisecond, and the crowded-arm boundary is where the previous
    head left it.
    """
    import time
    pinned = {"oneOf": [{"type": "integer", "minimum": 0, "maximum": 0},
                        {"const": 0}]}
    started = time.perf_counter()
    synth.instance(pinned, seed="s")
    assert time.perf_counter() - started < 0.5

    crowded = {"oneOf": [{"type": "number", "minimum": 0, "maximum": 10}
                         for _ in range(48)] + [{"const": "z"}]}
    assert synth.instance(crowded, seed="s") == "z"


def test_a_refused_ring_does_not_spend_the_node_budget():
    """A leaf whose whole ring is inadmissible stops walking.

    Without that, the refused `_holds` calls are charged to the node budget
    like any other, and a `oneOf` of 63 narrow numeric bands spent enough of
    it on checks that could never help to turn a schema which ANSWERED into
    `SchemaTooLarge`. Decision 117's narrowing direction, introduced by the
    round-55 widening and caught by a construction from round 52.
    """
    schema, answer = _rejected_fraction_schema(synth.MAX_CANDIDATES * 2)
    got = synth.instance(schema, seed="s")
    assert got == answer, f"answered {got!r}, wanted {answer!r}"
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_one_leaf_cannot_spend_the_whole_neighbour_allowance():
    """The walk is bounded per leaf as well as across the request.

    The shared allowance counts candidates that were YIELDED, and a leaf
    whose neighbours are all rejected by the outer combinator gets each one
    refunded — so without a per-leaf cap one leaf walks its whole ring and
    every later leaf finds nothing. Here six integer leaves each offer
    integral neighbours that match all seven arms and lose; the answer
    `0.5` can only come from the last leaf, and only if the first six left
    it some allowance.

    Found by searching for a construction that separates the bound, after
    the audit reported the case uncaught — the round-49 lesson: a guard
    nothing exercises is either unguarded or unnecessary, and which one it
    is has to be established rather than assumed.
    """
    schema = {"oneOf": [{"type": "integer"} for _ in range(6)]
                       + [{"type": "number"}]}
    got = synth.instance(schema, seed="s")
    assert got == 0.5, f"answered {got!r}; the last leaf was starved"
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_the_walk_steps_to_a_representable_neighbour():
    """At large magnitudes the fixed offsets vanish into the float's own
    precision: `1e20 + 0.5 == 1e20`, so every neighbour the walk proposed
    WAS the value it started from, and the schema got back the endpoint its
    competing branch also matches (Codex P2).

    `_step_inside` already knew this — it was written for the same
    arithmetic at a BOUND, where `bound + 1.0` rounded back and answered
    with the endpoint an `exclusiveMinimum` forbids. Decision 109 a fourth
    time, and the first where the other place answering the question was
    already correct and one screenful away.
    """
    import math
    upward = {"oneOf": [{"type": "number", "minimum": 1e20}, {"const": 1e20}]}
    got = synth.instance(upward, seed="s")
    assert got == math.nextafter(1e20, math.inf), f"answered {got!r}"
    assert synth._holds(got, upward, [synth.MAX_NODES])

    downward = {"oneOf": [{"type": "number", "maximum": -1e20},
                          {"const": -1e20}]}
    got = synth.instance(downward, seed="s")
    assert got == math.nextafter(-1e20, -math.inf), f"answered {got!r}"
    assert synth._holds(got, downward, [synth.MAX_NODES])


def test_a_large_integer_leaf_keeps_exact_steps():
    """An int loses nothing to precision — `value + 1` is exact however
    large it is — so the scale-aware fallback must not take over from it.

    Stepping by floats here answered `1.0000000000000002e+20` where the
    previous head answered the exact `100000000000000000001`: a worse
    answer to the same question, produced by a fix for the opposite
    problem. Only the fractional offsets vanish at this magnitude, and a
    half step from a huge integer is not representable anyway.
    """
    schema = {"oneOf": [{"type": "integer", "minimum": 1e20},
                        {"const": 1e20}]}
    got = synth.instance(schema, seed="s")
    assert got == 100000000000000000001, f"answered {got!r}"
    assert isinstance(got, int), f"answered {got!r}, which is not an int"
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_an_integer_past_the_float_range_does_not_crash():
    """Python ints are unbounded; floats are not.

    Round 56 decided an int leaf has no use for a fractional neighbour and
    checked that AFTER computing one — which works up to the float range
    and raises `OverflowError` past it. `10**400 + 0.5` is a gateway 500
    for a schema whose answer is simply `10**400 + 1` (Codex P2). The
    decision is made before the addition now.
    """
    for magnitude, wanted in ((10 ** 400, 10 ** 400 + 1),
                              (10 ** 309, 10 ** 309 + 1)):
        schema = {"oneOf": [{"type": "integer", "minimum": magnitude},
                            {"const": magnitude}]}
        got = synth.instance(schema, seed="s")
        assert got == wanted, f"answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])

    downward = {"oneOf": [{"type": "integer", "maximum": -(10 ** 400)},
                          {"const": -(10 ** 400)}]}
    got = synth.instance(downward, seed="s")
    assert got == -(10 ** 400) - 1, f"answered {got!r}"
    assert synth._holds(got, downward, [synth.MAX_NODES])


def _occupied_floats(start, count, *, upward=True):
    """`start` and its first `count` representable neighbours in one
    direction — the values a competing `enum` can occupy so that only the
    one past them is an exclusive witness."""
    out, cur = [start], start
    for _ in range(count):
        cur = math.nextafter(cur, math.inf if upward else -math.inf)
        out.append(cur)
    return out


def test_the_neighbour_walk_counts_candidates_not_attempts():
    """Past the float range of the fixed offsets, a ring's two same-signed
    offsets are the SAME neighbour.

    The ULP of `1e20` is 16384, so `1e20 + 0.5` and `1e20 + 1` both vanish,
    both route through `_step_inside`, and both land on the first
    representable float above. Charged twice, the per-leaf cap measured
    attempts rather than candidates and the walk stopped at the fourth step
    instead of the eighth — so with `1e20` and its first four upward floats
    occupied, the search fell back to `1e20`, which the schema rejects,
    though the fifth satisfies exactly one arm (Codex P2).
    """
    for upward in (True, False):
        start = 1e20 if upward else -1e20
        occupied = _occupied_floats(start, 4, upward=upward)
        schema = {"oneOf": [
            {"type": "number",
             ("minimum" if upward else "maximum"): start},
            {"enum": occupied},
        ]}
        witness = math.nextafter(occupied[-1],
                                 math.inf if upward else -math.inf)
        assert synth._holds(witness, schema, [synth.MAX_NODES]), (
            "the construction is wrong, not the code: the value past the "
            "occupied ones must be an exclusive witness"
        )
        got = synth.instance(schema, seed="s")
        assert got == witness, (
            f"{'upward' if upward else 'downward'}: answered {got!r}, "
            f"wanted {witness!r}"
        )


def test_a_repeated_enum_member_spends_no_candidate_capacity():
    """The same question as the walk, in the enum leaf's costume.

    A rejected member stopped charging in round 43 and a charged one has
    been refunded since round 39, and neither helps when every charge is
    for the SAME value: 64 copies of a member the consumer turns down
    exhausted the allowance and its refunds before a distinct 65th was
    reached, so `{"oneOf":[{"enum":[1…64 copies…,2]},{"const":1}]}`
    answered `1`, which matches both arms, though `2` matches one.
    """
    for copies in (synth.MAX_CANDIDATES,
                   synth.MAX_CANDIDATES * 2,
                   synth.MAX_CANDIDATES * 8):
        schema = {"oneOf": [{"enum": [1] * copies + [2]}, {"const": 1}]}
        got = synth.instance(schema, seed="s")
        assert got == 2, f"{copies} copies answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_repeated_arms_do_not_starve_the_arm_behind_them():
    """And in the arms loop's costume, which shares the same allowance.

    Each arm is a separate `_candidates` frame, so nothing inside one of
    them can see that the arm before it already produced this value. The
    record has to belong to the SEARCH, which is why `seen` is threaded
    through rather than kept per frame.
    """
    for arms in (synth.MAX_CANDIDATES,
                 synth.MAX_CANDIDATES * 2,
                 synth.MAX_CANDIDATES * 8):
        schema = {"oneOf": [
            {"anyOf": [{"const": 1}] * arms + [{"const": 2}]},
            {"const": 1},
        ]}
        got = synth.instance(schema, seed="s")
        assert got == 2, f"{arms} repeating arms answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_dropped_seed_gives_its_claim_back():
    """The claim is an allowance, and an allowance needs its release.

    `_candidates` has exactly one place that can claim a candidate and then
    drop it: the sibling-seed filter, which asks whether the seed satisfies
    the branch it was drawn for. A seed it turns down was never offered to
    anybody, so recording it as offered silences the arm whose answer it
    is — here the `integer` arm, whose `2` the seed filter had already
    claimed for a branch that could not use it. Left unreleased this
    regressed 159 schemas in a 40,000-schema search; it is the reason the
    release exists and the reason a widening fix gets measured in the
    narrowing direction.
    """
    schema = {"anyOf": [{"const": 2}],
              "oneOf": [{"oneOf": [{"const": 0.5}]}, {"type": "integer"}]}
    got = synth.instance(schema, seed="s")
    assert got == 2, f"answered {got!r}"
    assert synth._holds(got, schema, [synth.MAX_NODES])

    # The same shape with the repeat one level further out.
    other = {"oneOf": [{"oneOf": [{"const": 1.5}]}, {"type": "number"}],
             "anyOf": [{"enum": [-1e20]}]}
    got = synth.instance(other, seed="s")
    assert got == -1e20, f"answered {got!r}"
    assert synth._holds(got, other, [synth.MAX_NODES])


def test_every_candidate_charge_is_claimed_first():
    """Structural, and stated without an exception list.

    Three places charge a candidate counter, and each of them must first
    ask whether this search has already offered the value — otherwise the
    counter measures attempts. The rule is the one decision 109 keeps
    asking for: not "did I add the check where I was working", but "does
    every place that answers this question have it".
    """
    import pathlib
    import re

    source = pathlib.Path(synth.__file__).read_text(encoding="utf-8")
    lines = source.splitlines()
    # Comments and blanks are not code, and this rule is about code. A
    # `_claim` fifteen commented lines above its charge is still the guard
    # on that charge.
    code = [(n, ln) for n, ln in enumerate(lines, 1)
            if ln.strip() and not ln.strip().startswith("#")]
    charges = [i for i, (_, ln) in enumerate(code)
               if re.match(r"\s*\w*limit\[0\] -= 1\s*$", ln)]
    # FIVE since round 64 gave union-typed leaves their alternates: the
    # enum leaf, the non-enum leaf, the union member, the boolean value and
    # the numeric neighbour walk. The rule is unchanged and the count is not
    # the rule — it is here so that ADDING a charge site is a decision rather
    # than an accident, which is exactly what it was when the boolean block
    # went in at round 63 and the union block at round 64, each of which
    # turned this test red before the audit ran.
    assert len(charges) == 5, (
        f"expected five candidate charges, found {len(charges)}: "
        f"{[code[i] for i in charges]}"
    )
    for i in charges:
        window = [ln for _, ln in code[max(0, i - 4):i]]
        assert any("_claim(" in ln for ln in window), (
            f"line {code[i][0]} charges a candidate counter without "
            f"claiming the value first:\n" + "\n".join(window + [code[i][1]])
        )


def test_the_key_and_the_deep_comparison_agree():
    """The invariant the container dedup rests on, stated rather than
    trusted (decision 107).

    `_key` is an index over JSON Schema equality and `_same` is that
    equality spelled out, so wherever `_key` gives both values a key, the
    keys match exactly when `_same` says the values are the same. Get this
    wrong in one direction and a repeat goes on charging; get it wrong in
    the other and the search silences a candidate that is genuinely
    different, which is the worse of the two.

    The pairs below are chosen for the traps: `[1]` and `[true]` are equal
    to Python and different here, member order does not matter to an object
    and does to an array, and `1` and `1.0` are one JSON number at every
    depth.
    """
    values = [
        1, 1.0, True, False, None, "1", "",
        [], [1], [1.0], [True], [1, 2], [2, 1], [[1]], [[True]],
        {}, {"x": 1}, {"x": 1.0}, {"x": True}, {"x": 1, "y": 2},
        {"y": 2, "x": 1}, {"x": {"y": 1}}, {"x": {"y": True}},
        {"x": [1, {"y": None}]}, {"x": [1, {"y": None}]},
        [{"a": 1}], [{"a": True}], [None], ["a"], {"a": None},
    ]
    for left in values:
        for right in values:
            kl, kr = synth._key(left), synth._key(right)
            if kl is None or kr is None:
                continue
            same = synth._same(left, right)
            assert (kl == kr) is same, (
                f"_key and _same disagree on {left!r} vs {right!r}: "
                f"keys {'equal' if kl == kr else 'differ'}, _same={same}"
            )
            if kl == kr:
                assert hash(kl) == hash(kr), "equal keys must hash alike"


def test_a_repeated_container_spends_no_candidate_capacity():
    """The round-58 rule, in the shape round 58 did not cover.

    `_key` had no identity for a list or an object, so `_claim` called
    every container fresh and the starvation survived exactly where the
    values are containers: 64 copies of `{"x":1}` in an enum answered
    `{"x":1}`, which matches both arms, though `{"x":2}` behind them
    matches one (Codex P2). The array case and the arms costume failed the
    same way — the fix had been applied to three sites and none of them
    could see a repeated object.
    """
    for repeated, distinct in (({"x": 1}, {"x": 2}),
                               ([1], [2]),
                               ([{"a": [1]}], [{"a": [2]}]),
                               ({"a": [1, {"b": 2}]}, {"a": [1, {"b": 3}]})):
        for copies in (synth.MAX_CANDIDATES,
                       synth.MAX_CANDIDATES * 2,
                       synth.MAX_CANDIDATES * 8):
            schema = {"oneOf": [
                {"enum": [dict(repeated) if isinstance(repeated, dict)
                          else list(repeated) for _ in range(copies)]
                         + [distinct]},
                {"const": repeated},
            ]}
            got = synth.instance(schema, seed="s")
            assert got == distinct, (
                f"{copies} copies of {repeated!r} answered {got!r}"
            )
            assert synth._holds(got, schema, [synth.MAX_NODES])


def test_repeated_container_arms_do_not_starve_the_arm_behind_them():
    """The arms costume of the same thing, through separate frames."""
    for arms in (synth.MAX_CANDIDATES, synth.MAX_CANDIDATES * 8):
        schema = {"oneOf": [
            {"anyOf": [{"const": {"x": 1}}] * arms + [{"const": {"x": 2}}]},
            {"const": {"x": 1}},
        ]}
        got = synth.instance(schema, seed="s")
        assert got == {"x": 2}, f"{arms} repeating arms answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])


def test_a_container_repeat_is_judged_by_json_equality_not_pythons():
    """`[1]` and `[true]` are the same list to Python and different values
    here, so `==` would silence a candidate that is genuinely different.

    The bool-is-an-int trap for the seventh time in this file, and the
    reason the index carries a type tag at every depth rather than the
    bare value.
    """
    schema = {"oneOf": [{"enum": [[True]] * (synth.MAX_CANDIDATES * 2) + [[1]]},
                        {"const": [True]}]}
    got = synth.instance(schema, seed="s")
    assert got == [1] and got[0] is not True, f"answered {got!r}"
    assert synth._holds(got, schema, [synth.MAX_NODES])


def test_every_container_shape_is_indexed_not_scanned():
    """The index has to cover arrays, objects AND their nesting.

    Keying one shape and not another is not a half-fix, it is a fix that
    looks complete: the pairwise `_same` fallback still gets the right
    answer for whatever the index misses, so correctness alone cannot
    tell you a shape was left out. Only the COST can — the scan is
    quadratic in the enum and the index is linear — which is why this
    asserts that a 4,000-member intersection answers rather than
    exhausting the value allowance.

    Measured per shape, because the first version of the round-59 audit
    cases had one construction and caught only the array case: an object
    injection and a depth injection both passed a suite that had nothing
    holding them (decision 126, on my own guards this time).
    """
    shapes = (("array", lambda i: [i]),
              ("object", lambda i: {"a": i}),
              ("nested", lambda i: {"a": {"b": [i]}}))
    for label, make in shapes:
        schema = {"allOf": [{"enum": [make(i) for i in range(4_000)]},
                            {"enum": [make(i) for i in range(2_000, 6_000)]}]}
        got = synth.instance(schema, seed="s")
        assert synth._holds(got, schema, [synth.MAX_NODES]), (
            f"{label}: answered {got!r}"
        )


def test_keying_a_wide_value_is_memoised_and_charged():
    """Round 59 swapped one unbounded cost for another.

    Keying a container is O(its size), and the callers key the same
    inherited object over and over: a 50,000-member object reached
    through 31 overlapping arms was walked 64 times, none of it charged,
    and at 180,000 members that is twenty seconds of synchronous work the
    request-walk limit never sees and `_bounded_sync` cannot interrupt.

    Two halves, and BOTH are needed. The memo makes a repeat free, so the
    reported schema answers; the per-member charge makes the first walk
    visible, so a request cannot key unboundedly many wide values instead.
    """
    def wide(members, salt=0):
        return {f"k{salt}_{i}": i for i in range(members)}

    def crowded(values):
        return {"oneOf": [{"type": "object"} for _ in range(31)]
                         + [{"const": 1}],
                "anyOf": [{"enum": list(values) + [1]}]}

    # The memo: one walk, however many callers ask.
    budget = [synth.MAX_NODES]
    assert synth.instance(crowded([wide(20_000)]), seed="s",
                          budget=budget) == 1
    state = synth._value_state(budget)
    assert len(state["keys"]) == 1, "the object was keyed more than once"
    charged = synth.MAX_VALUE_WORK - state["allowance"][0]
    # TWO walks per distinct container, one each from the two questions
    # asked of a value: `_key` for its identity and `_representable` for
    # whether it can be written as JSON. Both are memoised, so it is two
    # walks per VALUE and not per call — the number to hold onto is that
    # the charge tracks the walk, so a request can key about
    # `MAX_VALUE_WORK / 2` members' worth of distinct containers and no
    # more. Charging per container instead made a 180,000-member object
    # cost four units, which is a memo pretending to be a bound.
    assert 40_000 <= charged < 40_100, (
        f"charged {charged} for two 20,000-member walks; the charge has "
        f"to track the walk, not the container"
    )

    # The charge: distinct wide values cannot be keyed without end.
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance(crowded([wide(180_000, 1), wide(180_000, 2)]),
                       seed="s")


def test_a_value_that_cannot_be_written_as_json_is_not_an_answer():
    """`1e999` parses to infinity and `json.dumps` writes it as the bare
    word `Infinity`, which no strict parser accepts.

    So `{"enum":[1e999,1]}` answered with a value the caller cannot read,
    though `1` is right there (Codex P2). A schema whose ONLY candidate is
    non-finite refuses instead, which is decision 108's better half: a
    refusal is an answer, and the right one when nothing can be written
    down.

    Nested too, because a guard that covers the reported shape and not the
    shape one level in looks complete and is not — round 59's lesson,
    applied while writing the fix rather than a round later.
    """
    inf, nan = float("inf"), float("nan")
    answers = [
        ({"enum": [inf, 1]}, 1),
        ({"enum": [-inf, 1]}, 1),
        ({"enum": [nan, 1]}, 1),
        ({"enum": [{"x": inf}, {"x": 1}]}, {"x": 1}),
        ({"enum": [[inf], [1]]}, [1]),
        ({"enum": [{"a": [{"b": inf}]}, {"a": [{"b": 1}]}]},
         {"a": [{"b": 1}]}),
    ]
    for schema, want in answers:
        got = synth.instance(schema, seed="s")
        assert got == want, f"{schema} answered {got!r}"
        assert json.dumps(got, allow_nan=False)

    for schema in ({"const": inf}, {"const": nan}, {"const": {"x": inf}},
                   {"enum": [inf, -inf]}):
        with pytest.raises(synth.Unsatisfiable):
            synth.instance(schema, seed="s")

    # A huge integer is a perfectly good JSON number and stays one —
    # `math.isfinite` would convert it and raise (§12 123).
    assert synth.instance({"enum": [10 ** 400]}, seed="s") == 10 ** 400
    assert synth.instance({"enum": [{"x": 10 ** 400}]},
                          seed="s") == {"x": 10 ** 400}


def test_a_speculative_probe_shares_the_value_state():
    """The invariant, stated rather than trusted (decision 107).

    A probe gets its own NODE count, so a merge that is thrown away costs
    no nodes. Everything else `_value_state` carries must be the same
    object: the allowance, so charged walks under a probe are charged to
    somebody, and the memo, so a probe can see what the live budget has
    already keyed.
    """
    budget = [synth.MAX_NODES]
    state = synth._value_state(budget)
    state["allowance"][0] = 12_345
    state.setdefault("keys", {})["sentinel"] = "held"

    probe = synth._probe(budget)
    assert probe[0] == budget[0], "a probe starts from the live node count"
    assert synth._value_state(probe) is state, (
        "a probe must share the value state, not make its own"
    )
    assert synth._value_state(probe)["allowance"][0] == 12_345
    assert synth._value_state(probe)["keys"]["sentinel"] == "held"

    # Nodes still roll back; value work does not. Spending in the probe
    # leaves the live node count alone and the live allowance charged.
    probe[0] -= 100
    synth._spend(synth._value_state(probe)["allowance"], "$")
    assert budget[0] == synth.MAX_NODES, "nodes must not leak out of a probe"
    assert state["allowance"][0] == 12_344, "value work must not be forgiven"


def test_speculative_merges_do_not_re_walk_an_inherited_value():
    """Round 60 bounded the key walk; round 61 found the door in it.

    Each of the three speculative-merge sites copied `budget[0]` into a
    fresh list, so `_value_state` handed the probe a brand new
    `MAX_VALUE_WORK` and an EMPTY memo — every arm re-walked the same
    inherited object, charged to nobody. A 180,000-member object under 31
    arms took 5.4 seconds while the live allowance reported 180,035 units.

    Counted at the charge rather than on the clock, because a timing
    assertion is a flake waiting to happen: with the state shared, the
    total charged tracks the object ONCE, not once per arm.
    """
    members = 2_000
    arms = 31
    huge = {f"k{i}": i for i in range(members)}
    schema = {"enum": [huge, 1, 2],
              "oneOf": [{"const": 1} for _ in range(arms)] + [{"const": 2}],
              "anyOf": [{"type": "integer"}]}

    charged = []
    real_spend = synth._spend

    def counting_spend(budget, path="$"):
        charged.append(1)
        return real_spend(budget, path)

    synth._spend = counting_spend
    try:
        got = synth.instance(schema, seed="s")
    finally:
        synth._spend = real_spend

    assert got == 2, f"answered {got!r}"
    # One walk of the object, not one per arm. The slack covers the rest
    # of the search; the point is the order of magnitude.
    assert len(charged) < members * 3, (
        f"{len(charged)} charges for a {members}-member object under "
        f"{arms} speculative arms — it is being re-walked per arm"
    )


def test_every_speculative_probe_goes_through_the_helper():
    """Structural, and stated without an exception list.

    FOUR sites build a speculative budget and all four must share the
    value state. A fifth written by hand would silently reopen the door,
    so the rule is that nobody constructs one inline.
    """
    import pathlib
    import re

    source = pathlib.Path(synth.__file__).read_text(encoding="utf-8")
    inline = re.findall(r"probe\s*=\s*\[budget\[0\]\]", source)
    assert not inline, (
        "a speculative budget was built by hand; use `_probe(budget)` so "
        "the value allowance and the key memo are shared"
    )
    # The USES, pinned exactly — not `>= 3` against the bare name, which
    # is the same defect decision 131 named in the sibling test one round
    # earlier and which was still sitting here: `def _probe(budget):`
    # matches too, so three call sites plus a definition satisfied a
    # lower bound of three even with a call site deleted. A count is not
    # a check unless you know what it counts, and a LOWER BOUND is not a
    # census. Round 65 added the fourth site, which is how this was found.
    calls = source.count("probe = _probe(budget)")
    assert calls == 4, (
        f"{calls} sites build a speculative budget through the helper; "
        f"there are four, and a new one is a decision rather than an "
        f"accident"
    )
    # And each of them COMMITS what it spent once the speculative work
    # succeeded. A probe isolates the FAILURE — the point is that a
    # discarded attempt costs nothing — but work that was kept and used
    # is work the request really did, and leaving it on the probe makes
    # it free. Measured on the round-65 site: a union member built from a
    # 400-item array cost 1,213 nodes committed and 811 uncommitted, so
    # roughly a third of the build went uncharged.
    #
    # Pinned structurally rather than behaviourally on purpose. The
    # behavioural difference is a RATIO, because `_holds` charges the
    # live budget either way, and a test that asserts "about a third
    # more" is a gate that will lie the first time the constant factor
    # moves. This says the thing that is actually invariant.
    # Counted as CALLS, by a pattern the definition line cannot match —
    # `_settle(budget, probe, allowed)` is a substring of
    # `def _settle(budget, probe, allowed):`, which is decision 136(f)
    # lying in wait for the third time. The `^\s+` is what excludes it.
    settles = re.findall(r"^\s+_settle\(budget, probe, allowed\)$",
                         source, re.M)
    assert len(settles) == calls, (
        f"{calls} probe sites but {len(settles)} settle what they spent; "
        f"a probe that succeeded did real work and must pay for it"
    )
    # And each site records what its probe was ALLOWED, because the
    # spend is the difference between that and the remainder. Round 67
    # narrowed one probe and left `budget[0] = probe[0]` alone, so a
    # four-node build under a sixty-four-node probe replaced a request's
    # remaining 10,000 nodes with 48 (Codex P2). The remainder and the
    # spend are the same statement only while every probe starts at the
    # live budget's own value, and one no longer does.
    allowances = re.findall(r"^\s+allowed = probe\[0\]$", source, re.M)
    assert len(allowances) == calls, (
        f"{calls} probe sites but {len(allowances)} record what the probe "
        f"was allowed; without it the spend cannot be computed"
    )


def test_value_exhaustion_is_not_swallowed_by_a_speculative_merge():
    """Round 61 made a probe share the value allowance; this is the bill.

    A speculative merge catches `SchemaTooLarge` and moves on, because a
    probe that runs out of NODES has only said this option was too big to
    be worth building. Once `_key` and `_representable` charge the SHARED
    allowance, the same exception can mean the REQUEST is over — and the
    handlers went on retrying against a budget with nothing left.

    Forced at the handler rather than through a schema, and that is worth
    being plain about: no schema was found that reaches this, because in
    practice the next charged operation re-raises and one escapes. What is
    demonstrated here is the handler's contract, which is where the defect
    is.
    """
    schema = {"oneOf": [{"type": "integer"}, {"type": "string"}],
              "anyOf": [{"const": 2}]}
    budget = [synth.MAX_NODES]
    state = synth._value_state(budget)
    real_merged = synth._merged
    calls = {"n": 0}

    def exhausting_merged(branches, parent, budget_arg=None):
        calls["n"] += 1
        if calls["n"] == 1 and budget_arg is not None:
            state["allowance"][0] = -1
            raise synth.SchemaTooLarge("$")
        return real_merged(branches, parent, budget_arg)

    synth._merged = exhausting_merged
    try:
        with pytest.raises(synth.SchemaTooLarge):
            synth.instance(schema, seed="s", budget=budget)
    finally:
        synth._merged = real_merged

    # And a probe that runs out of its OWN nodes is still survivable —
    # the distinction is the whole point, not a blanket re-raise.
    assert not synth._value_exhausted([synth.MAX_NODES])


def test_the_two_refusals_are_never_caught_together():
    """Structural, and stated without an exception list.

    `Unsatisfiable` from a speculative merge is always survivable: that
    option cannot be built. `SchemaTooLarge` is survivable only when it is
    the probe's own node count — so the two must not share a handler, or
    the request's exhaustion is indistinguishable from one option being
    too big.
    """
    import pathlib

    source = pathlib.Path(synth.__file__).read_text(encoding="utf-8")
    assert "except (Unsatisfiable, SchemaTooLarge)" not in source, (
        "the two refusals are caught together somewhere; a shared-value "
        "exhaustion would be swallowed as though one option were too big"
    )
    # The USES, not the definition — which also matches the bare name and
    # made an earlier version of this assertion pass with one guard
    # removed. The audit said so: the injection that drops the check at
    # the nested-seed site went NOT CAUGHT while this test stayed green.
    uses = source.count("if _value_exhausted(budget):")
    assert uses == 4, (
        f"{uses} speculative-merge handlers ask whether the request's "
        f"value allowance ran out; there are four, and each must"
    )


def test_a_contradiction_nested_in_allof_does_not_survive_distribution():
    """`_distributed`'s precheck read only an option's TOP-LEVEL `type`.

    An option whose conflict is inside `allOf` has no `type` of its own,
    so it passed the check and `_merged` coerced it into a usable
    product — and coercion is the right forgiveness for a schema the
    CALLER wrote, not for a combination this module invented. The schema
    answered `true`, violating its own `anyOf`, though `0.5` satisfies it
    (Codex P2).
    """
    cases = [
        ({"allOf": [{"anyOf": [{"const": True}, {"const": 0.5}]}],
          "anyOf": [{"allOf": [{"type": "boolean"}, {"type": "number"}]},
                    {"const": 0.5}]}, 0.5),
        ({"allOf": [{"anyOf": [{"const": "a"}, {"const": 1}]}],
          "anyOf": [{"allOf": [{"type": "string"}, {"type": "integer"}]},
                    {"const": 1}]}, 1),
        # And one level deeper, because a precheck that looks one level
        # in is the same defect with a longer reach.
        ({"allOf": [{"anyOf": [{"const": True}, {"const": 0.5}]}],
          "anyOf": [{"allOf": [{"allOf": [{"type": "boolean"}]},
                               {"type": "number"}]},
                    {"const": 0.5}]}, 0.5),
    ]
    for schema, want in cases:
        got = synth.instance(schema, seed="s")
        assert got == want, f"{schema} answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])

    # A declared type ALONGSIDE a contradictory nested `allOf`. This is
    # the only shape that needs the early return: with `type` absent the
    # empty set propagates on its own, so a fix without it looks complete
    # and a test without this case agrees — the audit said so, reporting
    # the injection NOT CAUGHT while three constructions passed.
    declared_and_contradictory = {
        "allOf": [{"anyOf": [{"const": "a"}, {"const": 0.5}]}],
        "anyOf": [{"type": "string",
                   "allOf": [{"allOf": [{"type": "boolean"},
                                        {"type": "number"}]}]},
                  {"const": 0.5}],
    }
    got = synth.instance(declared_and_contradictory, seed="s")
    assert got == 0.5, f"answered {got!r}"
    assert synth._holds(got, declared_and_contradictory, [synth.MAX_NODES])
    assert synth._effective_type(
        {"type": "string",
         "allOf": [{"allOf": [{"type": "boolean"}, {"type": "number"}]}]}
    ) == set()

    # `integer` narrows under `number` rather than colliding, so this one
    # is NOT a contradiction and must still build.
    fine = {"allOf": [{"type": "number"}, {"type": "integer"}]}
    assert synth._effective_type(fine) == "integer"
    assert synth._effective_type({"allOf": [{"type": "boolean"},
                                            {"type": "number"}]}) == set()
    assert synth._effective_type({"type": "string"}) == "string"
    assert synth._effective_type({}) is None


def test_a_boolean_leaf_offers_its_other_value():
    """The third kind of leaf with a finite choice.

    "Offer the choices, not only the one `instance` picked" reached enum
    leaves in round 43 and numeric leaves in round 54 and stopped there,
    so `{"oneOf":[{"type":"boolean"},{"const":true}]}` fell back to `true`
    — which matches both arms — though `false` matches exactly the
    boolean one (Codex P2). The census for "which leaves have a choice"
    had two entries where it should have had three.
    """
    schema = {"oneOf": [{"type": "boolean"}, {"const": True}]}
    got = synth.instance(schema, seed="s")
    assert got is False, f"answered {got!r}"
    assert synth._holds(got, schema, [synth.MAX_NODES])

    # The mirror already worked and must keep working: `true` matches the
    # boolean arm alone when it is `false` that is taken.
    mirrored = {"oneOf": [{"type": "boolean"}, {"const": False}]}
    assert synth.instance(mirrored, seed="s") is True

    # Through a sibling, and against an enum that carries the lookalike:
    # `true` and `1` are one value to Python and two here.
    sibling = {"oneOf": [{"type": "boolean"}, {"type": "integer"}],
               "anyOf": [{"const": False}]}
    assert synth.instance(sibling, seed="s") is False
    lookalike = {"oneOf": [{"type": "boolean"}, {"enum": [True, 1]}]}
    got = synth.instance(lookalike, seed="s")
    assert synth._holds(got, lookalike, [synth.MAX_NODES]), f"{got!r}"


def test_the_boolean_alternate_is_bounded_like_the_numeric_ones():
    """It is the same kind of candidate — one a leaf offers beyond what
    `instance` built — so it draws on the same allowance and gets the
    same bounded refund, and the census that pairs every candidate
    counter with a refund keeps reading true.
    """
    budget = [synth.MAX_NODES]
    schema = {"oneOf": [{"type": "boolean"}, {"const": True}]}
    assert synth.instance(schema, seed="s", budget=budget) is False
    state = synth._value_state(budget)
    assert "neighbour" in state and "neighbour_refunds" in state, (
        "the boolean alternate must use the shared neighbour accounting"
    )


def test_a_union_typed_leaf_offers_every_declared_type():
    """The FOURTH kind of leaf with a finite choice.

    A union IS the choice, and `instance` commits to one member and
    answers to itself rather than to the caller, so
    `{"oneOf":[{"type":["integer","array"]},{"type":"integer"}]}` came
    back `0` — which matches both arms — though `[]` matches exactly the
    union arm (Codex P2). Every union-beside-subset pair had the same
    shape, and none of them needs an impossible arm anywhere in it.
    """
    nodes = [synth.MAX_NODES]
    schema = {"oneOf": [{"type": ["integer", "array"]}, {"type": "integer"}]}
    got = synth.instance(schema, seed="s")
    assert isinstance(got, list), f"answered {got!r}"
    assert synth._holds(got, schema, nodes)

    # The same shape in three other unions, because a fix for one pair
    # that leaves the others wrong is a special case wearing a rule's
    # clothes.
    for union, subset, kind in (
        (["string", "number"], "string", (int, float)),
        (["boolean", "null"], "boolean", type(None)),
        (["object", "array"], "object", list),
    ):
        pair = {"oneOf": [{"type": union}, {"type": subset}]}
        answer = synth.instance(pair, seed="s")
        assert isinstance(answer, kind), f"{union} answered {answer!r}"
        assert synth._holds(answer, pair, [synth.MAX_NODES])


def test_the_union_alternate_is_bounded_like_the_other_leaves():
    """Same allowance, same bounded refund, no new capacity.

    A union member is the same kind of candidate as a numeric neighbour
    or a boolean's other value — one a leaf offers beyond what `instance`
    built — so decision 118's census (every allowance paired with a
    bounded refund) keeps reading true without an exception for it. It is
    finite by construction, seven type names at most, so unlike the
    numeric ring it needs no per-leaf step bound.
    """
    budget = [synth.MAX_NODES]
    schema = {"oneOf": [{"type": ["integer", "array"]}, {"type": "integer"}]}
    assert isinstance(synth.instance(schema, seed="s", budget=budget), list)
    state = synth._value_state(budget)
    assert "neighbour" in state and "neighbour_refunds" in state, (
        "the union alternate must use the shared neighbour accounting"
    )
    # It draws on the SAME counter the other alternates draw on, so a
    # search that has spent the neighbour allowance cannot then find
    # unlimited union members behind it.
    starved = [synth.MAX_NODES]
    starved_state = synth._value_state(starved)
    starved_state["neighbour"] = [0]
    starved_state["neighbour_refunds"] = [0]
    spent = synth.instance(schema, seed="s", budget=starved)
    assert not isinstance(spent, list), (
        "with the neighbour allowance spent the union member must not be "
        f"offered, got {spent!r}"
    )


def test_the_union_offers_null_last():
    """`null` is a real member and the least useful fixture, so it is
    ordered last rather than filtered out — the same order `instance`
    itself uses for a union, because two orders for one choice is a
    second thing to keep true by accident.

    The construction matters. My first attempt asserted that a real value
    wins, which BOTH orders satisfy whenever `null` is rejected for some
    other reason — so the injection that reorders the members left it
    green and it proved nothing. The order is only observable when the
    base value is rejected by the consumer and TWO members would both be
    accepted: here the integer matches both arms and is out, and `null`
    and a string are each admissible. Null-last answers the string;
    null-first answers `null`. Both are valid, which is why this pins a
    stated PREFERENCE rather than a correctness rule, and says so.
    """
    schema = {"oneOf": [{"type": ["null", "integer", "string"]},
                        {"type": "integer"}]}
    got = synth.instance(schema, seed="s")
    assert synth._holds(got, schema, [synth.MAX_NODES]), f"answered {got!r}"
    assert got is not None, (
        "null is declared first but must still be offered last, so the "
        "string is the answer"
    )

    # And it is still reachable when it is the only member that fits:
    # last is not excluded.
    only = {"oneOf": [{"type": ["null", "integer"]}, {"type": "integer"}]}
    assert synth.instance(only, seed="s") is None


def test_dropping_an_impossible_product_keeps_the_other_arms_answerable():
    """The round-64 report, which is the round-63 fix's own bill.

    Dropping an impossible product also dropped the array that product
    happened to build, and that array was the only value reaching a
    DIFFERENT arm's union type. Restoring the accident was the wrong fix:
    the union arm has to be able to offer an array on its own, which is
    what the test above pins. This one pins the composite the report
    actually named, so the two cannot drift apart.
    """
    schema = {"oneOf": [
        {"allOf": [{"anyOf": [{"type": "array", "allOf": [{"type": "string"}]}]},
                   {"anyOf": [{}]}]},
        {"oneOf": [{"type": ["integer", "array"]}, {"type": "integer"}]}]}
    got = synth.instance(schema, seed="s")
    assert synth._holds(got, schema, [synth.MAX_NODES]), f"answered {got!r}"
    # And the impossible product is still dropped — round 63 stands.
    assert isinstance(got, list), f"answered {got!r}"


def test_an_oversized_union_member_does_not_bankrupt_the_search():
    """A union member is OPTIONAL — the union offers it, the schema does
    not require it — so one too big to build must cost this search
    nothing.

    Built against the live budget it raised `SchemaTooLarge` out of
    `_candidates` and refused a request whose witness was one arm away
    (Codex P2). Decision 131 with me as the caller it describes: the new
    consumer of `instance` caught the survivable refusal and let the
    other one through.
    """
    schema = {"oneOf": [{"type": ["integer", "array"], "minItems": 10000},
                        {"const": 0}]}
    got = synth.instance(schema, seed="s")
    assert synth._holds(got, schema, [synth.MAX_NODES]), f"answered {got!r}"
    assert got == 1, f"answered {got!r}"

    # And the probe's isolation must not become an excuse: a schema whose
    # ONLY reading is the oversized one still refuses rather than
    # inventing a cheap answer it cannot justify.
    alone = {"type": "array", "minItems": 10000, "items": {"type": "string"}}
    with pytest.raises((synth.SchemaTooLarge, synth.Unsatisfiable)):
        synth.instance(alone, seed="s", budget=[2000])


def test_union_alternates_do_not_starve_a_later_numeric_leaf():
    """The refund cap bounds an UNBOUNDED generator; the union loop is
    bounded by construction, so drawing on that cap was wrong.

    56 union arms whose integers the outer `oneOf` all rejects used to
    spend the shared allowance permanently once the cap ran out, and the
    numeric leaf behind them never got to offer the one exclusive witness
    (Codex P2). Decision 126 in its mirror form: the union alternate
    loosened no starvation guard and COMPETED for the capacity those
    guards protect.
    """
    schema = {"oneOf": [{"type": ["string", "integer"], "minimum": i}
                        for i in range(1, 57)]
              + [{"type": "integer"}, {"type": "number"}]}
    got = synth.instance(schema, seed="s")
    assert synth._holds(got, schema, [synth.MAX_NODES]), f"answered {got!r}"
    assert got == 0.5, f"answered {got!r}"

    # The arm count is not the rule, so the boundary is checked either
    # side of the old cap rather than at one number that happened to
    # fail.
    for arms in (31, 32, 33, 64):
        wide = {"oneOf": [{"type": ["string", "integer"], "minimum": i}
                          for i in range(1, arms + 1)]
                + [{"type": "integer"}, {"type": "number"}]}
        answer = synth.instance(wide, seed="s")
        assert synth._holds(answer, wide, [synth.MAX_NODES]), (
            f"{arms} arms answered {answer!r}"
        )


def test_a_rejected_union_member_costs_the_search_nothing():
    """Net permanent spend is 0 for a leaf whose members are all rejected
    and 1 for one whose member is taken — what every other candidate
    source costs. Stated as the invariant rather than inferred from the
    answer above, because the answer can come out right for the wrong
    reason.
    """
    budget = [synth.MAX_NODES]
    state = synth._value_state(budget)
    state["neighbour"] = [synth.MAX_CANDIDATES]
    state["neighbour_refunds"] = [synth.MAX_CANDIDATES]
    before = state["neighbour"][0]

    # Every union member here is contextually rejected: the outer `oneOf`
    # is satisfied only by the fraction the numeric leaf offers.
    schema = {"oneOf": [{"type": ["string", "integer"], "minimum": i}
                        for i in range(1, 41)]
              + [{"type": "integer"}, {"type": "number"}]}
    assert synth.instance(schema, seed="s", budget=budget) == 0.5
    spent = before - state["neighbour"][0]
    assert spent <= 2, (
        f"40 rejected union members spent {spent} of the shared "
        f"allowance permanently; a rejected candidate is not a candidate"
    )


def test_duplicate_union_members_are_offered_once():
    """JSON Schema does not require `type` array members to be unique and
    nothing on the request path checks, so the module must.

    Round 65 gave each member a probe whose spend is discarded when the
    build is too big — right for one oversized member, wrong for a
    hundred copies, since each copy paid the full build again and charged
    nothing (Codex P1).
    """
    assert synth._union_members(["integer", "array", "array", "integer"]) == [
        "integer", "array"
    ]
    # Order is first-seen, and `null` still goes last however often it
    # appears or wherever it appears.
    assert synth._union_members(["null", "integer", "null", "string"]) == [
        "integer", "string", "null"
    ]
    # Non-strings are not members at all, deduplicated or otherwise.
    assert synth._union_members(["array", 3, "array", None]) == ["array"]
    assert synth._union_members([]) == []


def test_repeated_oversized_members_cost_one_probe_not_one_per_copy():
    """Measured at the counter, not the clock (decision 116): a wall-clock
    assertion is a flake waiting for a loaded runner, and the counter is
    what the bound is actually about.

    The answer must be identical at one copy and at two hundred, and so
    must the number of discarded probes it took to get there.
    """
    used = {}
    for copies in (1, 200):
        budget = [synth.MAX_NODES]
        schema = {"oneOf": [{"type": ["integer"] + ["array"] * copies,
                             "minItems": 10000}, {"const": 0}]}
        assert synth.instance(schema, seed="s", budget=budget) == 1
        state = synth._value_state(budget)
        used[copies] = synth._MAX_DISCARDED_MEMBER_PROBES - state[
            "discarded_members"][0]
    assert used[1] == used[200], (
        f"one copy cost {used[1]} discarded probes and two hundred cost "
        f"{used[200]}; deduplication is what makes those the same number"
    )


def test_discarded_member_probes_are_bounded_across_the_request():
    """Deduplication bounds the repeats WITHIN one leaf. Nothing bounded
    them ACROSS leaves, and each arm of a `oneOf` can offer its own
    oversized member, so the work grew with the arm count while nothing
    was charged for it — a second of event loop at sixty arms, and the
    gateway synthesises synchronously.

    A discarded probe is free by design, so that one member too big to
    build cannot bankrupt a request with a cheap witness. Free is only
    safe while the NUMBER of them is bounded.
    """
    def arms(n):
        return {"oneOf": [{"type": ["integer", "array"], "minItems": 10000,
                           "items": {"type": "integer"}, "minimum": i}
                          for i in range(1, n + 1)]
                + [{"type": "integer"}, {"type": "number"}]}

    # A schema of a reasonable size is untouched by the bound and still
    # finds the one exclusive witness.
    small = [synth.MAX_NODES]
    assert synth.instance(arms(4), seed="s", budget=small) == 0.5
    spent = synth._MAX_DISCARDED_MEMBER_PROBES - synth._value_state(
        small)["discarded_members"][0]
    assert spent < synth._MAX_DISCARDED_MEMBER_PROBES, (
        f"a four-arm schema spent {spent} discarded probes; the bound is "
        f"meant to bite on abuse, not on ordinary work"
    )

    # A schema built to abuse it stops at the bound rather than paying
    # per arm.
    big = [synth.MAX_NODES]
    try:
        synth.instance(arms(120), seed="s", budget=big)
    except (synth.Unsatisfiable, synth.SchemaTooLarge):
        pass
    left = synth._value_state(big)["discarded_members"][0]
    assert left == 0, (
        f"120 arms left {left} discarded probes unspent; the bound should "
        f"have been reached"
    )


def test_union_deduplication_is_linear_in_the_names():
    """`name not in names` on a LIST is linear per name and quadratic
    overall, and nothing on the request path limits how many names a
    `type` array may carry: 20,000 distinct names took 1.7s and 40,000
    took 7.2s, before a single node of the synthesis budget was charged
    (Codex P1). The bound added the round before counts DISCARDED BUILDS
    and never sees work done deciding what to build.

    Asserted structurally, because the property is "a set does the
    membership test" and the behavioural form is a wall-clock assertion —
    a flake waiting for a loaded runner, and decision 136(g) says to pin
    the invariant rather than the constant.
    """
    import inspect

    source = inspect.getsource(synth._union_members)
    assert "seen = set()" in source and "name not in seen" in source, (
        "the membership test must be against a set; a list makes this "
        "quadratic in the number of names"
    )
    # And it still answers exactly as before: order is first-seen, `null`
    # is last, repeats collapse.
    assert synth._union_members(
        ["string", "null", "string", "integer", "null"]
    ) == ["string", "integer", "null"]
    # A large one is answered at all, which is the point.
    big = [f"x{i}" for i in range(5000)] + ["integer", "x0"]
    assert synth._union_members(big)[-1] == "integer"
    assert len(synth._union_members(big)) == 5001


def test_the_probe_cap_narrows_the_probe_it_does_not_stop_offering():
    """Exhausting the discarded-probe allowance must bound the builds that
    may be DISCARDED, not terminate alternate generation.

    A `break` meant eight oversized arms in front of
    `{"type":["integer","boolean"]}` suppressed the `true` that satisfies
    exactly that arm, and the search answered an invalid `1` — a bound
    deciding an ANSWER rather than a cost (Codex P2). Measured at the
    counter, the threshold sat exactly on it: seven arms answered `true`
    and eight did not.
    """
    def arms(n):
        return {"oneOf": [{"type": ["integer", "array"], "minItems": 10000,
                           "items": {"type": "integer"}, "minimum": i}
                          for i in range(1, n + 1)]
                + [{"type": ["integer", "boolean"]}, {"type": "integer"}]}

    # Either side of the allowance, because a threshold that is only
    # checked on one side is not checked.
    for n in (7, 8, 9):
        schema = arms(n)
        got = synth.instance(schema, seed="s")
        assert got is True, f"{n} oversized arms answered {got!r}"
        assert synth._holds(got, schema, [synth.MAX_NODES])

    # And the allowance really is spent by then — otherwise the case
    # above proves nothing about the exhausted path.
    budget = [synth.MAX_NODES]
    synth.instance(arms(9), seed="s", budget=budget)
    assert synth._value_state(budget)["discarded_members"][0] == 0


def test_a_narrowed_probe_still_builds_a_cheap_member():
    """Sixty-four nodes, measured before the number was chosen: every
    scalar member builds in 1 node and the small containers in 4, while
    an array of ten thousand needs 10,001. So a cheap member is still
    offered once the allowance is gone, and an expensive one fails after
    64 rather than 10,001.

    The constant is asserted against what it must admit, not against
    itself — a test that says `_MAX_CHEAP_MEMBER_NODES == 64` would pass
    for any value anyone edits it to.
    """
    for cheap in ({"type": "boolean"}, {"type": "integer"},
                  {"type": "string"}, {"type": "null"},
                  {"type": "object"}, {"type": "array"},
                  {"type": "array", "minItems": 3,
                   "items": {"type": "integer"}}):
        budget = [synth._MAX_CHEAP_MEMBER_NODES]
        synth.instance(cheap, seed="s", budget=budget)

    # And an oversized one does NOT fit, which is what makes the narrowed
    # probe a bound rather than a formality.
    with pytest.raises(synth.SchemaTooLarge):
        synth.instance({"type": "array", "minItems": 10000,
                        "items": {"type": "integer"}},
                       seed="s", budget=[synth._MAX_CHEAP_MEMBER_NODES])

    # That the narrowing is APPLIED is pinned structurally, and the audit
    # is what said it had to be. Deleting the two lines that narrow the
    # probe leaves every behavioural assertion green: a discarded probe
    # never touches the live budget, so there is no counter to read
    # afterwards, and the only observable is wall-clock cost — which
    # decision 136(g) says to pin structurally rather than as a constant
    # that lies on a loaded runner. The injection that removes the
    # narrowing now dies here.
    import inspect

    source = inspect.getsource(synth._candidates)
    assert "probe[0] = min(probe[0], _MAX_CHEAP_MEMBER_NODES)" in source, (
        "the exhausted discarded-probe allowance must NARROW the probe; "
        "without it a post-allowance failure costs a full node budget "
        "again and the bound stops bounding anything"
    )


def test_a_narrowed_probe_settles_its_spend_not_its_remainder():
    """`budget[0] = probe[0]` is right only while every probe starts at
    the live budget's own value — then the remainder and the spend are
    the same statement. Round 67 narrowed one probe to
    `_MAX_CHEAP_MEMBER_NODES` and left the commit alone, so a four-node
    build under a sixty-four-node probe replaced a request's remaining
    10,000 nodes with 48: the whole allowance spent on a scalar, and a
    schema that answered a valid array before refused after (Codex P2).
    """
    budget = [synth.MAX_NODES]
    # Force the narrowed path: the discarded allowance is already gone.
    synth._value_state(budget)["discarded_members"] = [0]
    before = budget[0]
    schema = {"oneOf": [{"type": ["integer", "array"], "minItems": 3,
                         "items": {"type": "integer"}},
                        {"type": "integer"}]}
    synth.instance(schema, seed="s", budget=budget)
    spent = before - budget[0]
    assert spent < synth._MAX_CHEAP_MEMBER_NODES, (
        f"a cheap member under a narrowed probe spent {spent} nodes of "
        f"the live budget; the spend is the difference the probe used, "
        f"not whatever the narrowed probe had left"
    )

    # And the schema the report named answers again.
    reported = {"oneOf": [{"type": ["integer", "array"], "minItems": 10000,
                           "items": {"type": "integer"}, "minimum": i}
                          for i in range(1, 9)]
                + [{"type": ["integer", "array"], "minItems": 3,
                    "items": {"type": "integer"}},
                   {"type": "array"}, {"type": "integer"},
                   {"const": "winner"}]}
    got = synth.instance(reported, seed="s")
    assert synth._holds(got, reported, [synth.MAX_NODES]), f"answered {got!r}"


def test_an_unnarrowed_probe_settles_exactly_as_it_always_did():
    """For a probe that was never narrowed, `allowed` IS `budget[0]` and
    `_settle` is precisely the line it replaced. Stated as a test because
    three of the four sites depend on it and a helper that quietly
    changed them would be a worse defect than the one it fixed.
    """
    budget = [synth.MAX_NODES]
    probe = synth._probe(budget)
    allowed = probe[0]
    probe[0] -= 40                      # the probe spent forty nodes
    synth._settle(budget, probe, allowed)
    assert budget[0] == synth.MAX_NODES - 40

    # A probe narrowed first settles the same way: the spend, not the
    # remainder.
    budget = [synth.MAX_NODES]
    probe = synth._probe(budget)
    probe[0] = min(probe[0], synth._MAX_CHEAP_MEMBER_NODES)
    allowed = probe[0]
    probe[0] -= 4
    synth._settle(budget, probe, allowed)
    assert budget[0] == synth.MAX_NODES - 4
