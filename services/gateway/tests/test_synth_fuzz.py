"""Schemas this module was never shown, generated rather than written.

`test_synth_validates.py` checks a corpus somebody thought of. Twice now
a reviewer has found a keyword in its `HONOURED` list with no case
behind it — the property-merge miss I found by injecting an old defect,
and then four in one round. The corpus is written by the same blindness
that wrote the code, so the useful complement is schemas nobody chose.

Deterministic on purpose. `random.Random(seed)` over a fixed seed range
means a failure here is reproducible from its seed and can become a
named case in the corpus; a fuzzer that finds something once and never
again is worse than none. Stdlib only — the gateway takes no test
dependency for this.

Every schema is satisfiable BY CONSTRUCTION: bounds are ordered, enums
carry a value that clears their siblings, `oneOf` branches are disjoint
by type. So there are exactly two ways to fail — a refusal, which means
this module cannot serve a schema it should, or a document `jsonschema`
rejects, which means it produced one its own caller would throw away.
"""
from __future__ import annotations

import random

import jsonschema
import pytest

from gateway import synth

SCALARS = ("string", "integer", "number", "boolean", "null")


def _scalar(rng: random.Random, kind: str) -> dict:
    if kind == "string":
        schema: dict = {"type": "string"}
        if rng.random() < 0.4:
            low = rng.randint(0, 30)
            schema["minLength"] = low
            if rng.random() < 0.5:
                schema["maxLength"] = low + rng.randint(0, 80)
        return schema
    if kind in ("integer", "number"):
        schema = {"type": kind}
        low = rng.randint(-40, 40)
        if rng.random() < 0.7:
            # Exclusive and inclusive both, with room left between them
            # so the schema stays satisfiable whichever is chosen.
            schema["exclusiveMinimum" if rng.random() < 0.5 else "minimum"] = low
        if rng.random() < 0.5:
            schema["exclusiveMaximum" if rng.random() < 0.5 else "maximum"] = (
                low + rng.randint(5, 60)
            )
        return schema
    return {"type": kind}


def _value_for(rng: random.Random, kind: str):
    if kind == "string":
        return rng.choice(["alpha", "beta", "a longer value than the others"])
    if kind == "integer":
        return rng.randint(0, 5)
    if kind == "number":
        return rng.choice([0.5, 1.5, 2.0])
    if kind == "boolean":
        return rng.choice([True, False])
    return None


def _schema(rng: random.Random, depth: int = 0) -> dict:
    choice = rng.random()
    if depth < 2 and choice < 0.18:
        keys = [f"k{i}" for i in range(rng.randint(1, 3))]
        properties = {k: _schema(rng, depth + 1) for k in keys}
        schema: dict = {"type": "object", "properties": properties}
        if rng.random() < 0.6:
            schema["required"] = rng.sample(keys, rng.randint(1, len(keys)))
        if rng.random() < 0.2:
            # A required key `properties` never describes: valid, and the
            # shape that answered `{}` until round 27.
            schema.setdefault("required", []).append("undescribed")
        return schema
    if depth < 2 and choice < 0.24:
        # `allOf` over one array's ITEMS, and over `type` where one
        # branch narrows the other. Neither shape existed in the first
        # generator, so round 28's merge findings could not appear.
        if rng.random() < 0.5:
            low = rng.randint(5, 40)
            return {
                "allOf": [
                    {"type": "array", "items": {"type": "integer"}},
                    {"type": "array", "items": {"minimum": low}},
                ]
            }
        if rng.random() < 0.5:
            # Two type UNIONS with an exact match and a subtype overlap
            # both — the shape whose overlap was dropped in round 29.
            return {
                "enum": [rng.randint(1, 9)],
                "allOf": [
                    {"type": ["number", "string"]},
                    {"type": ["integer", "string"]},
                ],
            }
        return {
            "allOf": [
                {"type": "number", "minimum": rng.choice([0.5, 1.5, 2.5])},
                {"type": "integer"},
            ]
        }
    if depth < 2 and choice < 0.30:
        schema = {"type": "array", "items": _schema(rng, depth + 1)}
        if rng.random() < 0.6:
            low = rng.randint(0, 3)
            schema["minItems"] = low
            if rng.random() < 0.5:
                schema["maxItems"] = low + rng.randint(0, 3)
        return schema
    if depth < 2 and choice < 0.42:
        # `allOf` of two branches over one property, jointly satisfiable —
        # and with the BINDING constraint in the first branch, so a merge
        # that overwrites it with the second is visible. `minLength`
        # above the marker's own 52 characters is what makes it visible;
        # the first version of this generator used 0-20 and the round-21
        # property-merge defect walked straight through it.
        low = rng.randint(60, 120)
        return {
            "allOf": [
                {
                    "type": "object",
                    "properties": {"shared": {"type": "string", "minLength": low}},
                    "required": ["shared"],
                },
                {
                    "type": "object",
                    "properties": {
                        "shared": {"type": "string", "maxLength": low + 50}
                    },
                    "required": ["shared"],
                },
            ]
        }
    if depth < 2 and choice < 0.46:
        # `allOf` over two ENUMS sharing exactly one value, and not the
        # first of either — so first-wins is visible. Absent from the
        # first version, which is why round 24's intersection defect
        # survived it.
        if rng.random() < 0.4:
            # Boolean/number LOOKALIKES either side, sharing one real
            # value. To Python `True == 1`, so an intersection using `in`
            # keeps `true` and answers with it — invalid under the second
            # branch, which permits `1` and not `true`. Satisfiable: the
            # string is genuinely in both.
            return {
                "allOf": [
                    {"enum": [rng.choice([True, False]), "shared"]},
                    {"enum": [rng.choice([1, 0]), "shared"]},
                ]
            }
        shared = rng.choice(["keep", 7, True])
        others = [v for v in ["drop", 3, False] if type(v) is not type(shared)]
        return {
            "allOf": [
                {"enum": [others[0], shared]},
                {"enum": [others[-1], shared]},
            ]
        }
    if depth < 2 and choice < 0.52:
        if rng.random() < 0.5:
            # Disjoint by type, so exactly one branch is reachable.
            first, second = rng.sample(["string", "integer", "boolean"], 2)
            return {"oneOf": [{"type": first}, {"type": second}]}
        # OVERLAPPING branches, where the naive first-branch answer is
        # valid under both and `oneOf` therefore rejects it. A generator
        # producing only disjoint branches never exercises exclusivity
        # at all, which is how round 27's finding survived the first
        # version of this file.
        return {
            "oneOf": [
                {"type": rng.choice(["integer", "number"])},
                {"minimum": rng.randint(-5, 5)},
            ]
        }
    if depth < 2 and choice < 0.60:
        # A branch carrying constraints but NO type, so the parent's type
        # is load-bearing and a delegation that discards it shows up.
        kind = rng.choice(("string", "integer"))
        parent: dict = {"type": kind}
        if kind == "integer":
            low = rng.randint(5, 30)
            parent["minimum"] = low
            branch = {"maximum": low + rng.randint(5, 40)}
        else:
            parent["minLength"] = rng.randint(60, 100)
            branch = {"maxLength": 400}
        if rng.random() < 0.25:
            # A first option that cannot be BUILT at all (its own type
            # and enum disagree), with a workable one behind it. The
            # generator only ever produced options that synthesise, so a
            # branch raising mid-search could not appear — round 29.
            impossible = {
                "type": "string" if kind == "integer" else "integer",
                "enum": [1 if kind == "integer" else "x"],
            }
            return {**parent, rng.choice(["anyOf", "oneOf"]): [impossible, branch]}
        if rng.random() < 0.25:
            # A combinator NESTED in an allOf branch, whose first option
            # the sibling branch contradicts.
            return {
                "allOf": [
                    {"type": kind},
                    {
                        "anyOf": [
                            {"type": "string" if kind == "integer" else "integer"},
                            branch,
                        ]
                    },
                ]
            }
        if rng.random() < 0.4:
            # A FIRST option the parent contradicts, with a workable one
            # behind it. A generator whose combinator always had a
            # usable first branch never exercises branch selection at
            # all — round 28's finding.
            dead = {"type": "string" if kind == "integer" else "integer"}
            return {**parent, "anyOf": [dead, branch]}
        return {**parent, rng.choice(["anyOf", "oneOf"]): [branch]}
    if choice < 0.72:
        kind = rng.choice(SCALARS)
        values = [_value_for(rng, kind) for _ in range(rng.randint(1, 3))]
        schema = {"enum": values}
        if kind in ("integer", "number"):
            # A bound BESIDE the enum that actually EXCLUDES the first
            # member — which is the whole point, and which the first
            # version got wrong by setting the bound to the smallest
            # value, so it ruled nothing out and round 25's finding
            # could not appear. A sibling that excludes nothing is not a
            # sibling constraint, it is decoration.
            low, high = 1, 99
            schema = {"enum": [low, high], "minimum": high}
            return schema
        return schema
    if choice < 0.78:
        kind = rng.choice(SCALARS)
        return {"const": _value_for(rng, kind)}
    kind = rng.choice(SCALARS)
    schema = _scalar(rng, kind)
    if rng.random() < 0.25:
        # A `default` that may or may not fit: both are legal JSON Schema
        # and the synthesiser has to cope either way.
        schema["default"] = _value_for(rng, rng.choice(SCALARS))
    return schema


@pytest.mark.parametrize("seed", range(400))
def test_a_generated_schema_is_answered_and_the_answer_validates(seed):
    rng = random.Random(seed)
    schema = _schema(rng)
    try:
        produced = synth.instance(schema, seed=f"fuzz-{seed}")
    except synth.Unsatisfiable as refusal:
        raise AssertionError(
            f"seed {seed}: refused a schema built to be satisfiable at "
            f"{refusal.path}: {schema!r}"
        ) from None
    try:
        jsonschema.validate(produced, schema)
    except jsonschema.ValidationError as bad:
        raise AssertionError(
            f"seed {seed}: answered {produced!r} for {schema!r}, which its "
            f"own schema rejects — {bad.message}"
        ) from None


@pytest.mark.parametrize("seed", range(60))
def test_a_generated_schema_answers_the_same_way_twice(seed):
    schema = _schema(random.Random(seed))
    assert synth.instance(schema, seed="fixed") == synth.instance(schema, seed="fixed")
