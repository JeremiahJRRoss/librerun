"""Whatever the stub synthesises must satisfy the schema it came from.

Five rounds running have found a case where it did not: `allOf` treated
as a choice (decision 81), a property merge that kept one branch
(decision 84), a value landing exactly on `exclusiveMaximum` (85), the
same at a magnitude where `+ 1.0` does not move (87), `allOf` enums
taken first-wins (88), and an enum ignoring the bound beside it (89).
Every one was found by a reviewer reading one function, and every fix
was "handle one more keyword correctly".

The general statement of all six is one sentence — *the document this
module produces must validate against the schema it was given* — and
that is checkable, so it is checked here rather than argued about one
keyword at a time. `jsonschema` is a real validator, not this module's
own opinion of itself: using `_satisfies` to grade `instance` would
agree with itself about exactly the keywords it misunderstands.

The honest boundary is `NOT_HONOURED`. This module is a fixture
generator, not a solver, and it says so — it does not implement
`pattern`, `format`, `$ref` or the applicator keywords, and a schema
using one gets a well-formed value that may not satisfy it. Those are
listed with their reasons rather than quietly kept out of the corpus,
because a corpus that excludes what a module gets wrong proves nothing.
`HONOURED` is covered case by case, and the build fails if a keyword is
added to that set without a case that exercises it.

That gate checks a keyword APPEARS in some case, which is weaker than
checking it is tested — five rounds of review found keywords that were
claimed, present in a case, and still wrong. The harder question has its
own tool: `scripts/synth_keyword_audit.sh` injects one plausible defect
per honoured keyword and requires the suite to fail for each. All
fifteen currently bite. Run it after adding a keyword here, because
appearing in a case and being checked by one are different properties
and only the second is evidence.
"""
from __future__ import annotations

import jsonschema
import pytest

from gateway import synth

# What `instance()` reads and promises to respect.
HONOURED = {
    "type", "properties", "required", "items",
    "minItems", "maxItems", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "enum", "const", "allOf", "oneOf", "anyOf",
    "default",
}

# `oneOf` carries one caveat worth stating where it is claimed rather
# than in a commit message. "Exactly one branch" is a constraint over
# the OTHER branches, so honouring it means checking a candidate against
# all of them — and `_satisfies` judges only the keywords it implements,
# erring towards saying a branch is satisfied. Each branch is tried and
# the first candidate clearing exactly one is taken; if none can be SEEN
# to, the first branch's value stands rather than a refusal, because
# turning a satisfiable schema into an error is the worse failure
# (decision 90). Every case below finds an exclusive candidate.

# What it deliberately does not, and why. A schema using one of these
# still gets a well-formed reply; it may simply not satisfy that
# keyword, which is the documented limit of a fixture.
NOT_HONOURED = {
    "pattern": "a regex is a solver's problem; the marker string says "
               "plainly that it is a fixture rather than a match",
    "format": "same, and `format` is annotation-only in JSON Schema "
              "unless a validator opts in",
    "$ref": "no resolver on purpose — chasing references can loop, and a "
            "stub that hangs is worse than one that is approximate",
    "not": "satisfying a negation means searching the complement",
    "if": "applicator branches decide from a value that does not exist yet",
    "contains": "a constraint on which member, not on the shape",
    "propertyNames": "a pattern by another name",
    "uniqueItems": "the synthesiser repeats one item by construction",
    "dependentRequired": "conditional shape, same reason as `if`",
}

CASES: dict[str, dict] = {
    "a plain object": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["name", "count"],
    },
    "nested objects": {
        "type": "object",
        "properties": {
            "inner": {
                "type": "object",
                "properties": {"deep": {"type": "string"}},
                "required": ["deep"],
            }
        },
        "required": ["inner"],
    },
    "a bounded array": {
        "type": "array",
        "items": {"type": "string"},
        "minItems": 2,
        "maxItems": 4,
    },
    "a bounded string": {"type": "string", "minLength": 80, "maxLength": 120},
    "inclusive numeric bounds": {"type": "number", "minimum": -3.5, "maximum": 2.25},
    "exclusive numeric bounds": {
        "type": "number",
        "exclusiveMinimum": 0,
        "exclusiveMaximum": 0.5,
    },
    "an exclusive ceiling alone": {"type": "number", "exclusiveMaximum": 0},
    "an exclusive floor at scale": {"type": "number", "exclusiveMinimum": 1e20},
    "bounded integers": {"type": "integer", "minimum": 3, "exclusiveMaximum": 9},
    "an enum": {"enum": ["alpha", "beta"]},
    "a const": {"const": {"shape": "exact"}},
    "an enum its sibling narrows": {"enum": [1, 2, 3], "minimum": 2},
    "allOf over properties": {
        "allOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a"],
            },
            {
                "type": "object",
                "properties": {"b": {"type": "integer", "minimum": 4}},
                "required": ["b"],
            },
        ]
    },
    # Two branches constraining the SAME property, which is what a
    # recursive merge is for. The first corpus here had branches
    # touching DIFFERENT properties, so a merge that simply overwrote
    # one with the other passed every case — the round-21 defect walked
    # straight through the guard built to catch it. Found by injecting
    # that defect rather than by rereading the list.
    "allOf over one property twice": {
        "allOf": [
            {
                "type": "object",
                "properties": {"shared": {"type": "string", "minLength": 100}},
                "required": ["shared"],
            },
            {
                "type": "object",
                "properties": {"shared": {"type": "string", "maxLength": 200}},
                "required": ["shared"],
            },
        ]
    },
    "allOf over one numeric property twice": {
        "allOf": [
            {
                "type": "object",
                "properties": {"n": {"type": "integer", "minimum": 10}},
                "required": ["n"],
            },
            {
                "type": "object",
                "properties": {"n": {"type": "integer", "maximum": 12}},
                "required": ["n"],
            },
        ]
    },
    "allOf over bounds": {
        "allOf": [
            {"type": "string", "minLength": 40},
            {"type": "string", "maxLength": 60},
        ]
    },
    "allOf over enums": {
        "allOf": [{"enum": ["a", "b", "c"]}, {"enum": ["b", "c"]}]
    },
    "oneOf": {"oneOf": [{"type": "string", "minLength": 5}, {"type": "integer"}]},
    "anyOf": {"anyOf": [{"type": "integer", "minimum": 10}, {"type": "string"}]},
    # A union type, and an enum whose candidates are containers with
    # constraints INSIDE them. Both were absent from the first corpus,
    # and a reviewer found both defects in the hour after it was
    # written — the guard is only ever as good as the cases in it, which
    # is the same lesson the same file learned about the property merge
    # three cases up.
    "a union type": {"type": ["string", "integer"], "enum": [1]},
    "a union type reaching the other member": {
        "type": ["null", "string"],
        "enum": ["x"],
    },
    "an enum of arrays with an item schema": {
        "type": "array",
        "enum": [[1], ["x"]],
        "items": {"type": "string"},
    },
    "an enum of objects with properties": {
        "type": "object",
        "enum": [{"a": 1}, {"a": "s"}],
        "properties": {"a": {"type": "string"}},
    },
    "an enum of objects with a required key": {
        "type": "object",
        "enum": [{"b": 1}, {"a": 1}],
        "required": ["a"],
    },
    # Round 27's four, every one of which this corpus would have caught
    # had it held the case. A reviewer reading the HONOURED list above
    # is now the most productive way to extend it, which is the best
    # thing that list could be used for.
    "a required key with no property schema": {
        "type": "object",
        "required": ["x"],
    },
    "required keys only partly described": {
        "type": "object",
        "properties": {"a": {"type": "integer"}},
        "required": ["a", "b"],
    },
    "a default that does not fit": {"type": "integer", "default": "unknown"},
    "a default that does fit": {"type": "integer", "default": 42},
    "oneOf whose branches overlap": {
        "oneOf": [{"type": "integer"}, {"minimum": 0}]
    },
    "oneOf whose branches are disjoint": {
        "oneOf": [{"type": "integer"}, {"type": "string"}]
    },
    "a combinator beside its own constraints": {
        "type": "integer",
        "minimum": 10,
        "anyOf": [{"maximum": 20}],
    },
    "a boolean": {"type": "boolean"},
    "a null": {"type": "null"},
    "the shape an agent actually asks for": {
        "type": "object",
        "properties": {
            "severity": {"enum": ["low", "medium", "high"]},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "steps": {
                "type": "array",
                "items": {"type": "string", "minLength": 10},
                "minItems": 3,
            },
        },
        "required": ["severity", "confidence", "steps"],
    },
}


def _keywords(schema, found: set | None = None) -> set:
    found = set() if found is None else found
    if isinstance(schema, dict):
        for key, value in schema.items():
            found.add(key)
            _keywords(value, found)
    elif isinstance(schema, list):
        for item in schema:
            _keywords(item, found)
    return found


def test_every_honoured_keyword_is_exercised():
    """The gate. A keyword this module claims to respect, with no case
    proving it, is a claim nothing checks."""
    covered = set()
    for schema in CASES.values():
        covered |= _keywords(schema)
    missing = sorted(HONOURED - covered)
    assert not missing, (
        f"these keywords are honoured by synth and no case below exercises "
        f"them, so nothing here would notice them breaking: {missing}"
    )


def test_the_two_sets_do_not_overlap():
    overlap = sorted(HONOURED & set(NOT_HONOURED))
    assert not overlap, f"claimed and disclaimed at once: {overlap}"


@pytest.mark.parametrize("label", sorted(CASES))
def test_a_synthesised_document_validates_against_its_schema(label):
    schema = CASES[label]
    produced = synth.instance(schema, seed=f"seed-{label}")
    try:
        jsonschema.validate(produced, schema)
    except jsonschema.ValidationError as bad:
        raise AssertionError(
            f"{label}: the stub answered {produced!r}, which its own schema "
            f"rejects — {bad.message}"
        ) from None


@pytest.mark.parametrize("label", sorted(CASES))
def test_the_same_document_comes_back_for_the_same_seed(label):
    """Determinism is part of the contract: a keyless run is
    reproducible, and a smoke can assert on it."""
    schema = CASES[label]
    assert synth.instance(schema, seed="fixed") == synth.instance(
        schema, seed="fixed"
    )


@pytest.mark.parametrize("keyword", sorted(NOT_HONOURED))
def test_an_unhonoured_keyword_still_gets_a_well_formed_reply(keyword):
    """The limit is "may not satisfy it", never "raises" or "hangs". A
    schema using one of these gets a value; it simply may not match."""
    schema = {"type": "string", keyword: {"type": "string"}}
    assert isinstance(synth.instance(schema, seed="seed"), str)
