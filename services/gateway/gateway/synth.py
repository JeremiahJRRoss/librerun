"""Make a JSON document that satisfies a JSON Schema (keyless mode).

The stub provider has to answer a *structured* request — an agent that
asks for ``response_format: json_schema`` and then parses the reply —
without knowing anything about that agent. Synthesising an instance of
the schema it was handed is how: the reply is well-formed for whatever
agent asked, so keyless mode exercises the real pipeline (normalizers
included) instead of a special case, and no agent-specific fixture has
to live in the platform.

Deterministic by construction: the same schema and the same seed give
the same document, so a keyless run is reproducible and a smoke can
assert on it.

Every generated string carries the marker, because a report produced
this way is a smoke artifact and must never be mistaken for real work.
"""
from __future__ import annotations

import hashlib
import itertools
import math

STUB_MARKER = "[stub-llm fixture — no provider was called]"

_MAX_DEPTH = 24

# A fixture, not a dataset. Depth alone does not bound this: nested
# arrays multiply, so `minItems` 256 at each of 24 levels is 256**24
# values even with every individual array "capped". The bound has to be
# on the TOTAL, and it has to refuse rather than truncate — a reply that
# quietly carries fewer items than the schema demanded is exactly the
# shape a strict consumer rejects, which is the thing this module goes
# out of its way to avoid for numbers.
#
# A request may set `minItems: 1000000000` (valid JSON Schema, and
# keyless mode will authenticate anyone who has an agent key and a run
# token). Before this, that became `range(1000000000)` on the event
# loop: minutes of synchronous allocation, or the gateway's memory
# (Codex P2).
MAX_NODES = 10_000
# Comparing values is a different resource from walking structure, and
# charging one to the other is how the first attempt at this fix went
# wrong: index builds ate the node allowance, the search starved, and a
# construction that answered `1200` correctly began answering `0`, which
# the schema rejects. The node budget bounds the walk; this bounds the
# value comparisons and index builds underneath it. It matches the
# request-walk limit deliberately — a schema whose enums are larger than
# the walk that admitted it is not a schema this module needs to serve.
MAX_VALUE_WORK = 200_000
# How far a numeric leaf may walk away from the value it synthesised,
# looking for a neighbour the outer combinator will accept. Bounded per
# leaf so one leaf cannot spend the whole shared neighbour allowance
# (decision 121).
# A DISCARDED probe is free by design — that is the whole point of
# building an optional union member speculatively, so one member too big
# to construct cannot bankrupt a request with a cheap witness. "Free" is
# only safe while the NUMBER of them is bounded, and it was not: each arm
# of a `oneOf` can offer its own oversized member, so the work grew with
# the arm count while nothing was charged for it. Measured at 60 arms
# with a 10,000-element member each: 1.0s before this bound and flat
# after. Deduplicating the member names (the reported P1) fixes repeats
# WITHIN one leaf and says nothing about repeats ACROSS leaves.
#
# Eight, not thirty-two: a discarded probe produced nothing, so this is
# not capacity for candidates and must not be sized like it. Measured
# over 24,000 schemas from the round-64 and round-65 generators before
# the number was chosen — 23,573 use none, 427 use exactly one, and the
# most any schema that answers VALIDLY uses is one. Eight is eight times
# the observed maximum, so the bound bites only on shapes built to abuse
# it, and the worst case it still permits is eight discarded builds of
# `MAX_NODES` each rather than one per arm without limit.
_MAX_DISCARDED_MEMBER_PROBES = 8
# What a probe may spend once that allowance is gone. Enough for every
# scalar member (1 node) and every small container (4), and not enough
# for an array of ten thousand (10,001) — so the allowance bounds the
# COST of a discarded build rather than deciding which members are
# considered at all.
_MAX_CHEAP_MEMBER_NODES = 64
_MAX_NEIGHBOUR_STEPS = 8
# And how many of the shared neighbour allowance ONE leaf may take,
# so a single leaf cannot walk its whole ring and leave the next
# leaf nothing (decision 121).
_MAX_NEIGHBOUR_YIELDS = 8

# The same reasoning for one string. `minLength` is as unbounded as
# `minItems` was, and one value is enough: a gigabyte of characters is a
# single node the budget above would happily allow.
MAX_STRING = 65_536


class SchemaTooLarge(Exception):
    """Raised by ``instance`` when a schema asks for more than a fixture.

    Deliberately NOT a ``GatewayError``: this module synthesises JSON
    and knows nothing about HTTP. ``stub_provider`` translates it, the
    same way ``redaction`` translates its own ``_Refusal``.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


class Unsatisfiable(Exception):
    """Raised when a schema's own bounds exclude every value.

    A different failure from ``SchemaTooLarge``, which says "more than a
    fixture": this one says "nothing at all".
    ``{"type":"integer","exclusiveMinimum":0,"exclusiveMaximum":1}`` has
    no integer strictly inside it, and a fixture cannot be conjured for
    a schema nothing satisfies. Saying so and naming the path is the
    honest answer; the alternative is the defect this class exists to
    stop — returning a value that violates the schema the caller sent,
    which is what the old clamp did.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.path = path


def _is_length(value) -> bool:
    """An int, and not a bool. `True` is an int in Python, and a schema
    carrying `"minLength": true` should not mean "at least one"."""
    return isinstance(value, int) and not isinstance(value, bool)


# Bounds where `allOf` means "the tightest of them", not "the first".
#
# Split by what counts as a value for each, which is not the same test.
# `minLength`/`maxItems` are counts and only an int is one; `minimum`
# and `maximum` are numbers and `1.5` is a perfectly good bound. Using
# the length predicate for both meant fractional bounds were silently
# skipped: `allOf` with minima 1.5 and 2.5 synthesised 1.5, violating
# the second branch (Codex P2).
_TIGHTEN_COUNT = {
    "minLength": max, "minItems": max, "maxLength": min, "maxItems": min,
}
_TIGHTEN_NUMBER = {
    "minimum": max, "exclusiveMinimum": max,
    "maximum": min, "exclusiveMaximum": min,
}


def _is_number(value) -> bool:
    """An int or a float, and not a bool — `True` is neither a bound nor
    a length, however Python classifies it."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _spend(budget, path: str = "$") -> None:
    """Charge one unit to the shared allowance.

    Every site that does work proportional to the schema calls this. The
    budget is a one-element list so it is shared by reference down the
    whole call tree: decision 93's rule, which this module has now had to
    learn four times, is that a budget guards the work it is wrapped
    around, not the work that decides what to wrap.
    """
    budget[0] -= 1
    if budget[0] < 0:
        raise SchemaTooLarge(path)


def _effective_type(schema, depth: int = 0):
    """The type an option really constrains to, looking through `allOf`.

    A name, a list of names, `None` when nothing is declared, or an empty
    set when the declarations inside cannot all hold at once.

    `_distributed`'s precheck read only the TOP-LEVEL `type`, so an
    option whose contradiction is nested — `{"allOf":[{"type":"boolean"},
    {"type":"number"}]}` — has no `type` of its own, passed the check,
    and `_merged` then COERCED it into a usable product. The schema
    answered `true`, which violates its own `anyOf`, though `0.5`
    satisfies the whole thing (Codex P2).

    The empty set is the distinction `_type_overlap`'s docstring already
    draws and `_narrowed_type` deliberately declines to report: for a
    contradiction the CALLER wrote, this module produces the coherent
    thing rather than inventing a refusal; for a combination it invented
    itself, it drops the combination. This function serves the second
    caller, so it says "impossible" out loud.
    """
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None
    declared = schema.get("type")
    for branch in (schema.get("allOf") or []):
        inner = _effective_type(branch, depth + 1)
        if inner is None:
            continue
        if inner == set():
            return set()
        if declared is None:
            declared = inner
            continue
        common = _type_overlap(declared, inner)
        if common is None:
            continue
        if not common:
            return set()
        declared = common.pop() if len(common) == 1 else sorted(common)
    return declared


def _distributed(groups: list, budget) -> list:
    """Distribute several `anyOf` groups into one equivalent group.

    A value satisfies every group iff it satisfies one option from each,
    so the conjunction of disjunctions is the disjunction of the merged
    combinations — ordinary distribution. Carrying only the first group
    and dropping the rest is what returned `0` for `allOf` over
    `anyOf: [integer, string]` and `anyOf: [boolean, string]`: `0`
    satisfies the first group and violates the second, though `"a"`
    satisfies both (Codex P2).

    The cross product is charged to the shared budget per combination,
    so a schema that would explode here refuses instead of hanging.
    """
    combos: list = [{}]
    for _, options in groups:
        # Once per option and once per combination, not once per PAIR:
        # the effective type is a property of each, and the pairs
        # multiply. Decision 128's habit, applied while writing rather
        # than after a review.
        typed = []
        for option in options:
            kind = _effective_type(option)
            if kind == set():
                # The option contradicts ITSELF, nested inside `allOf`.
                # No combination containing it can be satisfied, and the
                # merge below would coerce it into something that looks
                # buildable.
                continue
            typed.append((option, kind))
        grown: list = []
        for combo in combos:
            combo_kind = _effective_type(combo)
            for option, kind in typed:
                _spend(budget, "$.anyOf")
                # A combination with disjoint types is impossible, and
                # `_merged` would COERCE it rather than say so: it keeps
                # the first `type` on a contradiction, which is the right
                # forgiveness for a schema the caller wrote and the wrong
                # one for a combination this function invented. Coerced,
                # `integer` x `boolean` became a plain `integer` and the
                # synthesiser answered `0` for a schema needing a string.
                if _type_overlap(combo_kind, kind) == set():
                    continue
                try:
                    grown.append(_merged([option], combo, budget))
                except Unsatisfiable:
                    # An unusable COMBINATION is not an unusable schema.
                    # The line above already skips a combination whose
                    # types are disjoint; a combination the merge itself
                    # declines — two `oneOf` groups meeting in one
                    # product, which decision 112 left refusing on
                    # purpose — is the same thing and was fatal instead.
                    # So `{"allOf":[{"anyOf":[{"const":0},{"oneOf":
                    # [{"const":1},{"const":1}]}]}, …same again…]}`
                    # raised after the valid `const`/`const` product had
                    # already been built, though `0` satisfies the
                    # schema (Codex P2).
                    #
                    # `SchemaTooLarge` is deliberately NOT caught: that
                    # is the allowance running out, which is a fact
                    # about the request rather than about this
                    # combination, and swallowing it would let a schema
                    # exceed its budget by attrition.
                    continue
                except SchemaTooLarge:
                    raise
        combos = grown
    if not combos:
        # Every combination was impossible, so the groups have no common
        # ground. Returning an empty `anyOf` would be worse than useless:
        # `instance` skips a combinator with no options, so the schema
        # would synthesise as though the groups were never there.
        raise Unsatisfiable("$.anyOf")
    return combos


def _merged(branches: list, parent: dict, budget=None) -> dict:
    """One schema satisfying every branch of an `allOf`, as far as this
    module can tell.

    Deliberately not a general JSON Schema merge — there is no such
    thing for arbitrary keywords. It covers what composition is actually
    used for: object fragments joined together, and bounds narrowed.

    - `properties` and `required` are unioned, so an object required by
      two branches carries the keys of both;
    - the bounds in `_TIGHTEN` take the tightest value, which is what
      `allOf` means for them;
    - a branch's own `allOf` is flattened in;
    - a branch that is a `oneOf`/`anyOf` contributes its first option,
      the same choice this module makes for those elsewhere;
    - everything else is first-wins, including `type`. Two branches
      declaring different types are a schema nothing can satisfy, and
      the choice there is the same one an unsatisfiable `minLength` /
      `maxLength` pair gets: produce the coherent thing rather than
      invent a refusal for the caller's own contradiction.
    """
    out: dict = {
        k: v for k, v in parent.items() if k not in ("allOf", "oneOf", "anyOf")
    }
    # The values every branch allows, intersected. `allOf` means all of
    # them, and `enum` was first-wins like any other non-bound keyword:
    # branches allowing `["a","b"]` and `["b"]` synthesised `"a"`,
    # violating the second branch although `"b"` satisfies the schema
    # (Codex P2). `const` is a one-element enum for this purpose, so the
    # two spellings intersect with each other rather than past each
    # other — the same lesson as `minimum`/`exclusiveMinimum`.
    if budget is None:
        # A merge reached directly (a test, or a nested call that has no
        # budget to hand) still gets one. The point is that it is SHARED
        # from here down, not that every entry point owns one.
        budget = [MAX_NODES]
    allowed = _allowed_values(out)
    # EVERY combinator, not the first of each kind. `carried[choice] =
    # options` kept one `anyOf` and silently dropped every later one, so
    # `allOf` over `anyOf: [integer, string]` and `anyOf: [boolean,
    # string]` answered `0` — satisfying the first group and violating
    # the second (Codex P2). The parent's own combinator joins the list
    # too: `out` strips it above, so it used to vanish with the rest.
    groups: list = []
    for choice in ("oneOf", "anyOf"):
        options = parent.get(choice)
        if isinstance(options, list) and options:
            usable = [o for o in options if isinstance(o, dict)]
            if usable:
                groups.append((choice, usable))
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        nested = branch.get("allOf")
        if isinstance(nested, list) and nested:
            branch = _merged(nested, branch, budget)
        # A combinator inside a branch is CARRIED, not collapsed. Taking
        # `options[0]` here threw away the search `instance` does for a
        # top-level one, so `allOf: [{"type":"integer"}, {"anyOf":
        # [{"type":"string"},{"minimum":5}]}]` answered `0` — the first
        # option conflicts with the integer branch and the compatible
        # second was discarded, though `5` satisfies the whole schema
        # (Codex P2). Preserved, `instance` re-enters with the merged
        # parent constraints and searches the options properly.
        for choice in ("oneOf", "anyOf"):
            # No `break`: a branch carrying BOTH kinds contributes both.
            # Stopping at the first kind dropped the other the same way.
            options = branch.get(choice)
            if isinstance(options, list) and options:
                usable = [o for o in options if isinstance(o, dict)]
                if usable:
                    groups.append((choice, usable))
                    branch = {k: v for k, v in branch.items() if k != choice}
        allowed = _intersect(allowed, _allowed_values(branch), budget)
        for key, value in branch.items():
            if key in ("allOf", "oneOf", "anyOf", "enum", "const"):
                continue
            if key == "properties" and isinstance(value, dict):
                # Recursive, not a shallow overwrite. Two branches
                # constraining the SAME property are the ordinary way to
                # say "a string, at least this long, at most that" — and
                # last-wins kept only the second, so a property with
                # `minLength: 100` in one branch and `maxLength: 200` in
                # another came back 52 characters long (Codex P2).
                merged_props = dict(out.get(key) or {})
                for name, sub in value.items():
                    existing = merged_props.get(name)
                    if isinstance(existing, dict) and isinstance(sub, dict):
                        try:
                            merged_props[name] = _merged([existing, sub], {}, budget)
                        except Unsatisfiable:
                            # A property nobody can satisfy is not an
                            # object nobody can satisfy — whether it
                            # sinks the object is a question about
                            # `required`, and `instance` already answers
                            # it: an optional property that cannot be
                            # built is left out, a required one takes
                            # the object down. Raising from the MERGE
                            # took that decision away and answered it
                            # the strict way every time, so
                            # `{"allOf":[{"properties":{"x":{"oneOf":
                            # [{"const":1}]}}},{"properties":{"x":
                            # {"oneOf":[{"const":2}]}}}]}` refused
                            # although `{}` satisfies it.
                            #
                            # `{"enum": []}` is this module's spelling
                            # for "no value is permitted", so the
                            # constraint is KEPT rather than dropped and
                            # the existing optional/required handling
                            # decides what it means.
                            #
                            # Found by walking the callers after
                            # decision 113 rather than by a review:
                            # a change to what a function refuses
                            # changes every caller that treats refusal
                            # as fatal.
                            merged_props[name] = {"enum": []}
                    else:
                        merged_props[name] = sub
                out[key] = merged_props
            elif key == "required" and isinstance(value, list):
                existing = out.get(key) or []
                out[key] = existing + [r for r in value if r not in existing]
            elif key == "items" and isinstance(value, dict) and isinstance(
                out.get(key), dict
            ):
                # An item schema is a schema, so two branches describing
                # the same array's items compose exactly as two branches
                # describing the same property do. First-wins dropped the
                # later one, so `items: {"type":"integer"}` beside
                # `items: {"minimum": 5}` answered `[0]` (Codex P2).
                try:
                    out[key] = _merged([out[key], value], {}, budget)
                except Unsatisfiable:
                    # And an item nobody can satisfy is not an array
                    # nobody can satisfy, exactly as a property nobody
                    # can satisfy is not an object: whether it sinks the
                    # array is a question about `minItems`, which the
                    # array branch already answers. Recorded as
                    # `{"enum": []}` so the constraint is kept and that
                    # existing logic decides.
                    #
                    # This is the sibling of the property merge four
                    # lines up — and the comment directly above says so
                    # in as many words. Decision 113(b) fixed the
                    # property half by walking the callers, and did not
                    # check the sibling spelling its own paragraph names
                    # (Codex P2). Rule 109 is not a rule I can apply to
                    # other people's code and skip on my own.
                    out[key] = {"enum": []}
            elif key == "type":
                out[key] = _narrowed_type(out.get(key), value)
            elif key in _TIGHTEN_COUNT and _is_length(value) and _is_length(out.get(key)):
                out[key] = _TIGHTEN_COUNT[key](out[key], value)
            elif key in _TIGHTEN_NUMBER and _is_number(value) and _is_number(out.get(key)):
                out[key] = _TIGHTEN_NUMBER[key](out[key], value)
            elif key not in out:
                out[key] = value
    if len(groups) == 1:
        choice, options = groups[0]
        out[choice] = options
    elif len(groups) > 1:
        # Several groups at one level. All-`anyOf` distributes exactly
        # (see `_distributed`). A `oneOf` among them does not: `oneOf`
        # is "exactly one option matches", and a combination picked from
        # it carries no promise that the siblings stay unmatched, so the
        # distributed form would assert something this module has not
        # checked. It refuses instead — the keyless stub declines the
        # shape by name (`schema_unsatisfiable`) rather than returning a
        # value that may violate the schema it was asked to satisfy. A
        # visible refusal is recoverable; a silent violation teaches an
        # author their schema holds when it does not.
        exclusive = [options for choice, options in groups if choice == "oneOf"]
        inclusive = [group for group in groups if group[0] != "oneOf"]
        if len(exclusive) > 1:
            # TWO `oneOf` groups is the ambiguous case the paragraph
            # above is about, and it still refuses. There is no way to
            # carry both: a schema has one `oneOf` key, and combining
            # them would assert the exclusivity this module has not
            # checked.
            raise Unsatisfiable("$.oneOf")
        if exclusive:
            # ONE `oneOf` beside some `anyOf` groups needs no
            # distribution and no refusal — it is carried as a SIBLING,
            # which is a shape this module already understands. The
            # combinator loop walks `oneOf` with everything else as
            # `siblings` and judges candidates against both; that is the
            # machinery decisions 102-105 built, and refusing here
            # ignored it. `{"anyOf":[{"oneOf":[{"const":2}],
            # "anyOf":[{"const":2}]}]}` was declined though `2`
            # satisfies it and the inner option alone answers `2`
            # (Codex P2).
            #
            # A refusal is right when a value might violate the schema.
            # It is wrong when the schema is SATISFIABLE and this module
            # simply took a shape it could not be bothered to keep —
            # decision 108's bar, applied to a refusal this batch wrote
            # on purpose and did not revisit.
            out["oneOf"] = exclusive[0]
        if len(inclusive) == 1:
            out["anyOf"] = inclusive[0][1]
        elif inclusive:
            out["anyOf"] = _distributed(inclusive, budget)
    if allowed is not None:
        # One spelling out, so the synthesiser cannot read a `const` that
        # the intersection has already ruled out. An empty list is a
        # schema no value satisfies; `instance` refuses on it by path.
        out.pop("const", None)
        out["enum"] = allowed
    return out


def _allowed_values(schema: dict):
    """The values this schema permits, as a list, or None when it names
    none. `const` is normalised to a one-element list.

    The enum is returned AS IT IS, not copied. The copy was defensive
    against a mutation no caller performs — every one of them either
    iterates the result or hands it to `_intersect`, which builds a new
    list and never writes through its arguments — and it cost more than
    it protected: a copy is a new object, so the index cache keyed by
    list identity missed on every derived schema and charged the whole
    enum again. With a 100,000-value sibling enum reachable only through
    the seed path, `_enum_index` ran 97 times over 3.3 million values,
    exhausted `MAX_VALUE_WORK` and refused a schema the previous head
    answered (Codex P2) — and one comfortably inside the 200,000-node
    admission limit.

    Nothing here may mutate a list it did not build.
    """
    if "const" in schema:
        return [schema["const"]]
    enum = schema.get("enum")
    if isinstance(enum, list):
        return enum
    return None


def _representable(value, budget=None, depth: int = 0) -> bool:
    """Can this value be written as JSON at all?

    Infinity and NaN cannot: `json.dumps` writes them as the bare words
    `Infinity` and `NaN`, which no strict parser accepts. A schema whose
    only candidate is one of those has no answer this module may give,
    and one with a usable candidate behind it must skip past.

    Containers too, and that is the whole reason this exists rather than
    a one-line test in `_satisfies`: `{"enum":[{"x":1e999},{"x":1}]}` has
    no non-finite value at the top and still answers with one. Round 59's
    lesson, applied to a fix while writing it rather than a round later —
    a guard that covers the reported shape and not the shape one level in
    looks complete and is not.

    Memoised and charged exactly as `_key` is, and for the same reason:
    this is a walk proportional to the value, the callers repeat it on
    the same inherited objects, and an uncharged repeat of a walk is the
    P1 decision 128 is about.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, int):
        # Unbounded in Python and a perfectly good JSON number. Asking
        # `math.isfinite` would convert it and raise (§12 123).
        return True
    if not isinstance(value, (list, dict)):
        return True
    if depth > _MAX_DEPTH:
        # Past what this module will walk. `_satisfies` refuses such a
        # value on its own account before it can matter here.
        return True
    memo = allowance = None
    if budget is not None:
        state = _value_state(budget)
        memo = state.setdefault("finite", {})
        held = memo.get(id(value))
        if held is not None and held[0] is value:
            return held[1]
        allowance = state["allowance"]
    members = value if isinstance(value, list) else value.values()
    ok = True
    for member in members:
        if allowance is not None:
            _spend(allowance, "$")
        if not _representable(member, budget, depth + 1):
            ok = False
            break
    if memo is not None:
        memo[id(value)] = (value, ok)
    return ok


def _same(left, right, depth: int = 0) -> bool:
    """JSON Schema equality, which is not Python's.

    `true` and `1` are different values in JSON Schema and the same one
    to Python — `True == 1`, and `True in [1]` — so intersecting
    `enum: [true]` with `enum: [1]` kept `true` and synthesised it for a
    schema nothing satisfies (Codex P2). That is the bool-is-an-int trap
    for the sixth time in this file, after `_is_length`, `_is_number`
    and three tests that could not fail.

    Recursive, because the lookalikes nest: `[1, 2]` and `[true, 2]`
    compare equal to Python element by element for the same reason.

    `1` and `1.0` ARE the same JSON number, so numbers compare by value
    once booleans are out of the way.
    """
    if depth > _MAX_DEPTH:
        # Both this and `_satisfies` walk VALUES out of the request body,
        # which `instance`'s own depth limit never sees. Python's
        # recursion limit is what stopped them, at about 900 levels, as
        # an uncaught RecursionError — a 500 from a body anyone holding
        # an agent key can send. I had reasoned that `json.loads` would
        # refuse such a body first and did not measure it; it parses 1200
        # levels without complaint, because it does not recurse in Python
        # for lists. Reasoning is not measuring.
        raise SchemaTooLarge("$")
    if isinstance(left, bool) or isinstance(right, bool):
        # Only a bool equals a bool, and only itself.
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, str) and isinstance(right, str):
        return left == right
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _same(a, b, depth + 1) for a, b in zip(left, right)
        )
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _same(left[k], right[k], depth + 1) for k in left
        )
    return False


def _narrowed_type(existing, incoming):
    """`allOf` over `type`: the types BOTH branches allow.

    First-wins here answered `0.5` for `allOf` of `{"type":"number",
    "minimum":0.5}` and `{"type":"integer"}` — a schema satisfiable by
    `1` (Codex P2). `integer` is a subset of `number`, so that pair
    narrows to `integer` rather than colliding.

    A genuinely empty intersection keeps the existing declaration, which
    is the choice this module already makes for contradictions: produce
    the coherent thing rather than invent a refusal for the caller's own
    impossible schema.
    """
    common = _type_overlap(existing, incoming)
    if common is None:
        return existing if existing is not None else incoming
    if not common:
        return existing
    return common.pop() if len(common) == 1 else sorted(common)


def _type_overlap(existing, incoming):
    """The types both declarations allow.

    `None` means "no pair of declarations to compare" (one side absent or
    empty); an empty set means the two are genuinely DISJOINT. The two
    callers want opposite things from that distinction, so it is computed
    once here rather than twice: `_narrowed_type` keeps the existing
    declaration for a caller's own contradictory `allOf`, while
    `_distributed` drops a combination it generated itself — a
    combination this module invented is not the caller's contradiction to
    be forgiving about.
    """
    if existing is None or incoming is None:
        return None
    names = {existing} if isinstance(existing, str) else set(existing or ())
    other = {incoming} if isinstance(incoming, str) else set(incoming or ())
    if not names or not other:
        return None
    common = names & other
    # The subtype overlap is ADDED, not used only as a fallback. Guarding
    # it behind `not common` meant a pair with an exact match as well —
    # `["number","string"]` against `["integer","string"]` — narrowed to
    # `string` alone and made `enum: [1]` unsatisfiable, though `1` is
    # valid under both branches (Codex P2). Two overlaps are two
    # overlaps; keeping one because the other exists is not an
    # intersection.
    if "number" in names and "integer" in other:
        common = common | {"integer"}
    if "integer" in names and "number" in other:
        common = common | {"integer"}
    return common


def _union_members(declared):
    """The type names a union declares, once each, `null` last.

    DEDUPLICATED, because JSON Schema does not require the members of a
    `type` array to be unique and nothing on the request path checks.
    Round 65 gave `_candidates` a PROBE whose spend is discarded when a
    member is too big to build — right for one oversized member and
    wrong for a hundred copies of it, because each copy paid the full
    build again and charged nothing:
    `{"oneOf":[{"type":["integer","array"…100 copies…],"minItems":10000},
    {"const":0}]}` took 2.4 seconds here and grew linearly with the
    copies, against an immediate refusal before (Codex P1). Synthesis
    runs synchronously, so that is the gateway's event loop.

    Decision 124 — already-offered is not new — reaching the member
    NAMES rather than the values. `_claim` deduplicates what a build
    RETURNS, and a build that fails returns nothing to deduplicate, so
    the repetition has to be stopped before the work rather than after
    it. Decision 127 again as well: only COST revealed this, since every
    answer was correct throughout.

    `null` LAST, not excluded. Filtering it out entirely meant
    `{"type":["null","string"],"minLength":2,"maxLength":1}` returned an
    invalid one-character string and the numeric equivalent refused
    outright, though `null` satisfies both, since length and range
    keywords do not apply to it (Codex P2). It goes last because a real
    value is the more useful fixture; it stays in the list because
    "prefer" is not "never".

    One helper for both callers, stated once rather than kept true twice
    — the same reason `_effective_type` exists. `instance` builds its
    members against the LIVE budget, so duplicates there were already
    self-limiting (a hundred oversized copies refuse in the same 0.02s
    as one); it shares this anyway, because two spellings of one rule is
    a second thing to keep true by accident, and because the duplicated
    lines had already made an audit anchor ambiguous once.
    """
    # A SET for the membership test, a list for the order. `name not in
    # names` on a list is linear per name and so quadratic overall, and
    # nothing on the request path limits how many names a `type` array
    # may carry: 20,000 distinct names took 1.7s here and 40,000 took
    # 7.2s, before a single node of the synthesis budget was charged
    # (Codex P1). The bound added last round counts DISCARDED BUILDS and
    # never sees work done deciding what to build.
    #
    # Recognised names are not filtered to the seven JSON Schema spells,
    # though that would bound the loop at seven: `{"type":["bogus"]}`
    # currently falls through to the declared path and refuses, and
    # dropping the name would send it to the untyped path to invent an
    # answer for a schema that has none. Linear is enough, and answers
    # stay exactly where they were.
    seen = set()
    names = []
    for name in declared:
        if isinstance(name, str) and name not in seen:
            seen.add(name)
            names.append(name)
    members = [name for name in names if name != "null"]
    if "null" in names:
        members.append("null")
    return members


def _settle(budget, probe, allowed):
    """Charge the live budget for what a successful probe actually SPENT.

    `budget[0] = probe[0]` was right for as long as every probe started
    at the live budget's own value: the remainder and the spend are then
    the same statement. Round 67 narrowed one probe to
    `_MAX_CHEAP_MEMBER_NODES` and left the commit alone, so a four-node
    build under a sixty-four-node probe replaced a request's remaining
    10,000 nodes with 48 — the whole allowance gone to pay for a
    scalar, and a schema that answered a valid array before refused
    after (Codex P2).

    So the spend is subtracted rather than the remainder assigned, and
    it is said once here rather than four times at the call sites. For
    an un-narrowed probe `allowed` IS `budget[0]` and this is exactly
    the old line; for a narrowed one it is the only form that can be
    right. A probe that fails never reaches here — that is what makes
    discarding it free.
    """
    budget[0] -= allowed - probe[0]


def _key(value, depth: int = 0, budget=None):
    """A hashable key carrying JSON Schema's type distinctions, or None
    for a value it cannot key.

    `("bool", True)` and `("number", 1)` are different keys, which is the
    whole point; `("number", 1)` and `("number", 1.0)` are the same one,
    because they are the same JSON number.

    Containers are keyed too, recursively. They were not, and every
    caller paid for it differently: `_enum_index`, `_member` and
    `_intersect` each kept a second list and compared it pairwise with
    `_same`, and `_claim` — the fourth asker, added last round — had no
    fallback at all and treated every repeat as new (Codex P2).

    Keying them is the cheaper half of that fix and the reason the
    dearer half stopped mattering: a charged pairwise scan is quadratic
    in the values offered and walks a whole value per comparison, which
    measurably turned schemas that ANSWERED into `SchemaTooLarge`. An
    index costs one walk per value instead.

    The key agrees with `_same` wherever both are defined, and the suite
    states that as an invariant rather than trusting it: objects sort by
    member name because `_same` compares `keys()` as a set, arrays keep
    their order because `_same` zips them, and the bool/int distinction
    is carried at every depth because `[1]` and `[true]` are equal to
    Python and different here.

    Past `_MAX_DEPTH` it gives up and returns None — the same bound
    `_same` raises at — so the pairwise fallback still exists for the
    values no index can hold, and is still charged.

    MEMOISED per request and CHARGED on the walk, because round 59
    swapped one unbounded cost for another and only measured the one it
    was looking at. Keying a container is O(its size), and the callers
    key the same inherited object over and over: a 50,000-member object
    reached through 31 overlapping arms was walked 64 times for 3.2
    million member visits, none of them charged, and at 180,000 members
    that is twenty seconds of synchronous work the request-walk limit
    never sees and `_bounded_sync` cannot interrupt (Codex P1).

    One walk per value per request, not per call — the memo is keyed on
    identity because the values come from the request and are stable
    within it, and it holds the value itself so the id stays meaningful
    and cannot be reused under it. The charge then bounds what is left:
    the total size of the distinct containers a request keys, which is
    what `MAX_VALUE_WORK` is for. A repeat costs nothing because it is
    not work.
    """
    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    if isinstance(value, str):
        return ("string", value)
    if value is None:
        return ("null",)
    if not isinstance(value, (list, dict)):
        return None
    memo = allowance = None
    if budget is not None:
        state = _value_state(budget)
        memo = state.setdefault("keys", {})
        held = memo.get(id(value))
        # `is`, not `==`: an id can be reused once the object it named is
        # gone, and the memo holding the value is what stops that here.
        if held is not None and held[0] is value:
            return held[1]
        allowance = state["allowance"]
    if isinstance(value, list):
        items = []
        for item in value:
            # Per MEMBER, not per container. Charging the container once
            # made a 180,000-member object cost four units, which is a
            # memo rather than a bound: what has to be bounded is the
            # total a request can WALK, and that is the sum of the sizes
            # of the distinct containers it keys.
            if allowance is not None:
                _spend(allowance, "$")
            key = _key(item, depth + 1, budget)
            if key is None:
                return None
            items.append(key)
        built = ("array", tuple(items))
    else:
        members = []
        for name in sorted(value):
            if allowance is not None:
                _spend(allowance, "$")
            key = _key(value[name], depth + 1, budget)
            if key is None:
                return None
            members.append((name, key))
        built = ("object", tuple(members))
    if memo is not None:
        memo[id(value)] = (value, built)
    return built


def _claim(value, seen, budget) -> bool:
    """Whether this search has not already offered `value` — recording it
    if not, so the next caller gets `False`.

    A candidate the search has already offered is not a new candidate,
    and must not spend the capacity meant for one. That is the same
    sentence the enum leaf and the unbuildable leaf already carry, in
    its third costume: rejected does not charge (round 43), unbuildable
    does not charge (round 44), and now neither does a repeat.

    Suppressing a repeat loses nothing, and the reason is worth stating
    because it is what makes this safe rather than merely cheaper: the
    consumer judges a candidate by the candidate alone — the exclusivity
    count over the original options, then the siblings — so the same
    value offered twice gets the same verdict twice, whichever branch,
    seed or neighbour produced it. The second offer cannot inform the
    search; it can only starve it.

    A list or an object has no `_key`, and the first version of this
    exempted every one of them — so a repeated container went on
    charging and the starvation this fixes survived in the one shape the
    fix did not cover: 64 copies of `{"x":1}` in an enum still answered
    `{"x":1}`, which matches both arms, though `{"x":2}` behind them
    matches one (Codex P2). The analogous array case failed the same
    way, and so did the arms costume.

    The right answer was three functions up. `_enum_index`, `_member`
    and `_intersect` all ask this same question — is this value one I
    have already got? — and all three answer it the same way: index by
    `_key` where the value is hashable, and fall back to a pairwise
    `_same` under a charge where it is not. This is the FOURTH place to
    ask it and the only one that got it wrong, which is decision 109
    exactly: census the file for the question, including the places that
    already answer it correctly.

    `_same` rather than `==` because JSON Schema equality is not
    Python's: `[1] == [True]` and `{"x":1} == {"x":True}` are both true
    to Python and both false here, so `==` would silence a candidate
    that is genuinely different. The bool-is-an-int trap for the
    seventh time in this file.

    CHARGED, for the reason `_intersect` was: the scan is quadratic in
    the containers offered and each comparison walks a whole value, so
    a wide enough value makes it the P1 that fix was written for. "Rare
    in practice" is not a bound.
    """
    keys, containers = seen
    key = _key(value, budget=budget)
    if key is not None:
        if key in keys:
            return False
        keys.add(key)
        return True
    allowance = _value_state(budget)["allowance"]
    for other in containers:
        _spend(allowance, "$")
        if _same(value, other):
            return False
    containers.append(value)
    return True


def _release(value, seen, budget) -> None:
    """Give a claim back, for a candidate that was claimed and then never
    reached the consumer.

    The claim is an allowance like the counters beside it, and it needs
    the same pairing they do: a candidate the search did not actually
    offer must not be recorded as offered. `_candidates` has exactly one
    place that can claim and then drop — the sibling-seed filter, which
    asks whether the seed satisfies the branch it was drawn for — and
    the release sits next to the capacity refund there, because they are
    the same sentence about the same candidate.

    Measured, not reasoned: without it, `{"anyOf":[{"const":2}],"oneOf":
    [{"oneOf":[{"const":0.5}]},{"type":"integer"}]}` answered `0.5`,
    which the sibling rejects, because the seed filter claimed `2` for a
    branch that could not use it and the `integer` arm behind it was
    then refused its own answer. 159 schemas in 40,000 regressed this
    way, and every one of them came back.

    Both stores, because a claim can have gone into either.
    """
    keys, containers = seen
    key = _key(value, budget=budget)
    if key is not None:
        keys.discard(key)
        return
    allowance = _value_state(budget)["allowance"]
    for i, other in enumerate(containers):
        _spend(allowance, "$")
        if _same(value, other):
            del containers[i]
            return


# The alternatives an untyped schema may be answered with when the
# inferred type cannot satisfy it. A real value first and `null` last,
# the same preference the type-union branch makes for the same reason.
_OTHER_TYPES = (0, "", [], {}, True, None)


def _other_type(schema, budget, avoid=()):
    """A value of some OTHER JSON type that satisfies the whole schema.

    `type` being absent is an INFERENCE, not a requirement. `minLength`
    and `maxLength` constrain strings and nothing else; `properties` and
    `required` constrain objects and nothing else. So a schema whose
    inferred type cannot satisfy it is not unsatisfiable — it is a
    schema about some other type, and
    `{"properties":{"x":{"type":"string","enum":[1]}},"required":["x"]}`
    raised `Unsatisfiable` at `$.x` though `0` satisfies it (Codex P2).

    Round 42 fixed exactly this for the string default and only for the
    string default. That made it a special case rather than a rule, and
    the object spelling was one keyword family away — the fifth time in
    this batch that a principle applied to one of two symmetric places
    had to be applied to the other afterwards. It is a helper now, so
    the next inferred type gets it for free.

    Returns `_NO_ALTERNATIVE` when nothing else fits either, which means
    the caller's schema really is contradictory and the caller decides
    what to do about that.
    """
    for alternative in _OTHER_TYPES:
        if any(alternative is skip for skip in avoid):
            continue
        if _holds(alternative, schema, budget):
            return alternative
    return _NO_ALTERNATIVE


class _NoAlternative:
    """Distinct from every JSON value, including `None`, which is one."""

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<no alternative>"


_NO_ALTERNATIVE = _NoAlternative()


def _value_state(budget):
    """The value-work allowance and index cache carried by a budget.

    Both hang off the budget list because that is already the one object
    shared by reference down the whole call tree and living exactly as
    long as the request — no new parameter to thread, and nothing
    retained once the request is gone. `_spend` only ever reads
    `budget[0]`, so the extra slot is invisible to it.
    """
    if len(budget) < 2:
        budget.append({"allowance": [MAX_VALUE_WORK]})
    return budget[1]


def _probe(budget):
    """A budget for speculative work: its OWN node count, and the SAME
    value state.

    Three sites try a merge they may throw away, and each copied
    `budget[0]` into a fresh list so a failed attempt costs no nodes.
    That is right for nodes and wrong for everything else `_value_state`
    carries: the probe got a brand new `MAX_VALUE_WORK` and an EMPTY key
    memo, so the charged walks under it were charged to nobody and the
    memo could not remember what the live budget had already keyed.

    Measured: a 180,000-member object inherited by 31 speculative arms
    took 5.4 seconds while the live allowance reported 180,035 units —
    the bound with a door in it, and the door is mine from round 60
    (Codex P1). Time grew with the members; the number the bound could
    see did not.

    The value spend is NOT rolled back when a probe fails, and that is
    the point rather than an oversight: the walk happened, the CPU went,
    and a bound that forgives work already done is not a bound. Nodes
    still roll back, because a node count is a measure of the tree a
    merge WOULD build and the merge was thrown away.
    """
    # Force the state into being first, so the probe shares it rather
    # than making its own on first use.
    _value_state(budget)
    return [budget[0], budget[1]]


def _value_exhausted(budget) -> bool:
    """Whether the request's shared value allowance is spent.

    A speculative merge catches `SchemaTooLarge` and moves on, because a
    probe that runs out of NODES has only told us this option was too big
    to be worth building — one unusable seed is not an unusable schema
    (decision 113(a)). That reading stopped being safe the moment
    `_probe` began sharing the value state: `_key` and `_representable`
    charge the shared allowance, so the same exception can now mean the
    REQUEST is over, and the handlers went on retrying against a budget
    that had nothing left (Codex P2).

    Decision 113 once more, and this time on a refusal I changed myself
    one round earlier: a change to what a function refuses changes every
    caller that treats refusal as fatal — and every caller that treats it
    as survivable.
    """
    return _value_state(budget)["allowance"][0] < 0


def _enum_index(permitted, budget):
    """A `_key` index over an enum's values, built ONCE per allowance.

    Charging each comparison is not enough on its own, and measuring
    proved it: the scan repeats once per candidate, so a 200-value enum
    that answers correctly today cost more than the whole allowance and
    began returning `SchemaTooLarge` — decision 90's over-eager refusal,
    caused by the very fix meant to bound the work. The cost has to be
    REMOVED, as `_intersect` removed it, not merely accounted for.

    So the index is built once and reused: the build is charged per
    value, which bounds it at the allowance and refuses a 180,000-value
    enum in about a millisecond instead of holding the event loop for
    seconds, and every membership test afterwards is a hash lookup that
    costs nothing.

    It is memoised on the BUDGET, which is already the one object shared
    by reference down the whole call tree and lives exactly as long as
    the request does — no new parameter to thread, and nothing retained
    once the request is gone. Holding the list itself in the entry is
    what makes keying on its identity safe: the object cannot be freed
    and its id reused while the entry is alive.
    """
    cache = _value_state(budget)
    hit = cache.get(id(permitted))
    if hit is not None and hit[0] is permitted:
        return hit[1], hit[2]
    allowance = cache["allowance"]
    hashable = set()
    unhashable = []
    for option in permitted:
        _spend(allowance, "$")
        key = _key(option, budget=budget)
        if key is None:
            unhashable.append(option)
        else:
            hashable.add(key)
    cache[id(permitted)] = (permitted, hashable, unhashable)
    return hashable, unhashable


def _member(value, permitted, budget, depth: int = 0) -> bool:
    """Is the value one of the permitted ones — indexed, and CHARGED.

    `_intersect` learned this in its own round: comparing every value
    against every other was `n * m` calls to a recursive `_same`, two
    disjoint 5,000-element enums took 7.3 seconds inside `instance()`,
    and the node budget never saw a unit of it (Codex P1). It was fixed
    there with a hash index over `_key` and a charge per value examined.

    `_satisfies` kept the linear `_same` scan, and it is reached once
    per CANDIDATE rather than once per merge, so the same cost came back
    multiplied: 32 candidates against a 180,000-value sibling enum spent
    1.7 seconds and 194 nodes — the time grew with the enum and the
    charge did not move at all. `stub_provider.complete` is synchronous
    end to end and `_bounded_sync` cannot interrupt synchronous work, so
    that is the gateway's event loop held by one authenticated request
    (Codex P1). A 180,000-value enum clears the 200,000-node request
    walk, so the request gets that far.

    A value Python cannot hash cannot use the index and is compared
    pairwise under the charge, exactly as `_intersect` does for the same
    values: "rare in practice" is not a bound.
    """
    allowance = _value_state(budget)["allowance"]
    key = _key(value, budget=budget)
    if key is None:
        for option in permitted:
            _spend(allowance, "$")
            if _same(value, option, depth):
                return True
        return False
    hashable, unhashable = _enum_index(permitted, budget)
    if key in hashable:
        return True
    for option in unhashable:
        _spend(allowance, "$")
        if _same(value, option, depth):
            return True
    return False


def _intersect(current, incoming, budget=None):
    """`allOf` over two value constraints: what both allow.

    Indexed, not quadratic. Comparing every value against every other
    was `n * m` calls to a recursive `_same`, and two disjoint
    5,000-element enums took **7.3 seconds inside `instance()`** —
    synchronous, on the gateway's event loop, from one authenticated
    request, which is every concurrent request stalled behind it (Codex
    P1). The node budget never saw it: the intersection happens during
    the merge, before a single node is built.

    Scalars go through a hash index, so the common case is `n + m` with
    no comparisons at all. Lists and objects cannot be hashed and still
    need `_same`, so those alone are compared pairwise — under a budget,
    because "rare in practice" is not a bound.
    """
    if incoming is None:
        return current
    if current is None:
        # Also not copied, and for the same reason: an intersection
        # against nothing IS the incoming list, and returning a fresh
        # object here breaks the index cache exactly as the defensive
        # copy in `_allowed_values` did.
        return incoming
    if budget is None:
        budget = [MAX_NODES]

    def spend():
        budget[0] -= 1
        if budget[0] < 0:
            raise SchemaTooLarge("$")

    index = set()
    unhashable = []
    for value in incoming:
        spend()
        key = _key(value, budget=budget)
        if key is None:
            unhashable.append(value)
        else:
            index.add(key)
    out = []
    for value in current:
        spend()
        key = _key(value, budget=budget)
        if key is not None:
            if key in index:
                out.append(value)
            continue
        for other in unhashable:
            spend()
            if _same(value, other):
                out.append(value)
                break
    return out


def _is_type(value, declared: str) -> bool:
    """Does `value` have the declared JSON Schema type?

    Infinity and NaN are not JSON numbers and have no type here. The
    request parser turns `1e999` into `inf`, `json.dumps` writes that as
    the bare word `Infinity`, and a strict parser rejects the structured
    response it lands in — so `{"enum":[1e999,1]}` answered with a value
    the caller cannot read, though `1` is right there (Codex P2).

    `_numeric` has refused non-finite BOUNDS since it was written and
    checks `math.isfinite` on the candidate it builds. This is the half
    that decides which values are OFFERED, and it never asked — the same
    split round 43 found between the loop that picks a value to return
    and the loop that decides what may be picked.

    `math.isfinite` only where the value is a float: it converts its
    argument, so `math.isfinite(10**400)` is the `OverflowError` of §12
    123 all over again. A huge int is a perfectly good JSON number and
    stays one.
    """
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        # `1.0` is an integer in JSON Schema; `True` is not.
        if isinstance(value, bool):
            return False
        if isinstance(value, float):
            return math.isfinite(value) and value.is_integer()
        return isinstance(value, int)
    if declared == "number":
        if isinstance(value, bool):
            return False
        if isinstance(value, float):
            return math.isfinite(value)
        return isinstance(value, int)
    if declared == "string":
        return isinstance(value, str)
    if declared == "array":
        return isinstance(value, list)
    if declared == "object":
        return isinstance(value, dict)
    if declared == "null":
        return value is None
    return True


def _satisfies(value, schema: dict, depth: int = 0, budget=None) -> bool:
    """Does this candidate clear the constraints this module understands?

    An `enum` names the permitted values and its SIBLINGS narrow them
    further: `{"enum": [1, 2], "minimum": 2}` permits only `2`, and
    returning `enum[0]` answered `1` — a fixture the caller's own
    validator rejects, for a schema with a perfectly good answer in it
    (Codex P2).

    Only rules this module can evaluate reject a candidate. An
    unfamiliar keyword must never silently eliminate a legal value:
    being unable to check something is not the same as having checked it.

    The budget is threaded through because the `enum` scan is real work
    proportional to the schema (see `_member`). It defaults to a fresh
    allowance so a direct call still works, exactly as `_merged` and
    `_intersect` do; the point is that it is SHARED from here down when
    a caller has one, not that every entry point owns one.
    """
    if budget is None:
        budget = [MAX_NODES]
    if depth > _MAX_DEPTH:
        # See `_same`: a value nested past what this module will
        # synthesise is refused rather than recursed into until Python
        # gives up with a 500.
        raise SchemaTooLarge("$")
    if not _representable(value, budget):
        # Infinity and NaN are not JSON, so no schema is satisfied by
        # one. The request parser turns `1e999` into `inf` and
        # `json.dumps` writes it as the bare word `Infinity`, which a
        # strict parser rejects — so `{"enum":[1e999,1]}` answered with a
        # value the caller cannot read, though `1` is right there (Codex
        # P2). `{"const":1e999}` now refuses instead, which is decision
        # 108's better half: a refusal is an answer, and it is the right
        # one when the only candidate cannot be written down.
        #
        # Here rather than in `_is_type`, because an `enum` need not
        # declare a type and this has nothing to do with the type it
        # declares: the value is not representable at all. `_numeric`
        # has refused non-finite BOUNDS since it was written; this is
        # the same question asked of the value, and of everything
        # inside it.
        return False
    declared = schema.get("type")
    if isinstance(declared, list):
        # A union: the value need match only ONE member. Reducing the
        # list to `declared[0]` refused `{"type": ["string","integer"],
        # "enum": [1]}` — a perfectly satisfiable schema turned into a
        # refusal (Codex P2). That is the failure this function is meant
        # to avoid in the OTHER direction, and worse than the defect it
        # came from: an over-eager check breaks agents that work today.
        names = [name for name in declared if isinstance(name, str)]
        if names and not any(_is_type(value, name) for name in names):
            return False
    elif isinstance(declared, str) and not _is_type(value, declared):
        return False
    # `enum` and `const` name the permitted values outright, and this
    # function could not see either: `_satisfies(1, {"enum": [2]})` was
    # True. That made every enum-only branch look satisfied when
    # counting `oneOf` matches, so `{"oneOf":[{"enum":[1]},
    # {"enum":[2,1]}]}` counted both candidates as matching twice and
    # fell back to `1` — which really does match both branches and is
    # invalid under `oneOf`, while branch 1 offers `2`, matching exactly
    # one (Codex P2).
    #
    # This is a rule the module CAN evaluate, so checking it is the
    # design rather than an exception to it: the docstring's promise is
    # that an unfamiliar keyword never eliminates a legal value, not
    # that a familiar one goes unchecked. `_same` does the comparing, so
    # `True` does not pass for `1` — that equivalence holds in Python
    # and not in JSON Schema, and it has cost this module several rounds.
    if "const" in schema and not _same(value, schema["const"], depth):
        return False
    permitted = schema.get("enum")
    if isinstance(permitted, list) and not _member(value, permitted, budget, depth):
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        low, low_open = _lower(schema)
        high, high_open = _upper(schema)
        if low is not None and not ((value > low) if low_open else (value >= low)):
            return False
        if high is not None and not ((value < high) if high_open else (value <= high)):
            return False
        return True
    if isinstance(value, (str, list)):
        least, most = ("minLength", "maxLength") if isinstance(value, str) else (
            "minItems", "maxItems"
        )
        floor, ceiling = schema.get(least), schema.get(most)
        if _is_length(floor) and len(value) < floor:
            return False
        if _is_length(ceiling) and len(value) > ceiling:
            return False
    # A container's own constraints reach INSIDE it, so the check has to
    # as well. `{"type":"array","enum":[[1],["x"]],"items":{"type":
    # "string"}}` answered `[1]`, and an enum object could violate the
    # `properties` beside it — the scalar version of this was the
    # previous round's finding, and stopping at the container was the
    # same mistake one level up (Codex P2).
    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict) and not all(
            _satisfies(element, items, depth + 1, budget) for element in value
        ):
            return False
    if isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            for name, sub in properties.items():
                if name in value and isinstance(sub, dict):
                    if not _satisfies(value[name], sub, depth + 1, budget):
                        return False
        required = schema.get("required")
        if isinstance(required, list) and not all(
            isinstance(name, str) and name in value for name in required
        ):
            return False
    return True


def _lower(schema: dict):
    """The effective lower bound, as ``(value, exclusive)``.

    ``minimum`` and ``exclusiveMinimum`` are one bound in two spellings,
    and reading them BOTH is this function's job. A merge-time
    reconciliation used to do it instead — and only for schemas that had
    been through an ``allOf`` merge, so a plain
    ``{"minimum": 0, "exclusiveMinimum": 5}`` never passed through it and
    the synthesiser read ``minimum`` first and answered 0, under a bound
    stated in the same object.

    Deciding it at the point of READING covers both cases with one rule,
    which left the merge-time version doing nothing at all: removing it
    changed no test in the suite, so it is gone rather than kept as a
    second answer to a question that now has one (the same finding as
    decision 73's ``close()``).
    """
    closed, open_ = schema.get("minimum"), schema.get("exclusiveMinimum")
    if _is_number(closed) and _is_number(open_):
        # `> 5` is at least as tight as `>= 5`, so ties go to exclusive.
        return (open_, True) if open_ >= closed else (closed, False)
    if _is_number(open_):
        return open_, True
    if _is_number(closed):
        return closed, False
    return None, False


def _upper(schema: dict):
    """The effective upper bound, as ``(value, exclusive)``."""
    closed, open_ = schema.get("maximum"), schema.get("exclusiveMaximum")
    if _is_number(closed) and _is_number(open_):
        return (open_, True) if open_ <= closed else (closed, False)
    if _is_number(open_):
        return open_, True
    if _is_number(closed):
        return closed, False
    return None, False


def _to_float(value) -> float:
    """A bound as a float, without raising.

    An integer too large for a float becomes the infinity it already
    means to a float-valued schema: no `number` can be at least
    ``10**400``. `float()` raises OverflowError on one, and an uncaught
    OverflowError is a 500 from a schema anyone with an agent key can
    send (Codex P2).
    """
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _step_inside(bound: float, *, upward: bool) -> float:
    """The first value strictly past ``bound``, in the given direction.

    A whole unit where a unit is visible, and the next representable
    float where it is not. `bound + 1.0` is what this used to be, and at
    large magnitudes it rounds straight back: ``1e20 + 1.0 == 1e20``, so
    an `exclusiveMinimum` of ``1e20`` was answered with ``1e20`` — the
    endpoint the schema forbids (Codex P2). Trying the unit first keeps
    the readable answers (``exclusiveMaximum: 0`` still gives ``-1.0``
    rather than a denormal) and falls back only where it must.
    """
    unit = bound + 1.0 if upward else bound - 1.0
    if (unit > bound) if upward else (unit < bound):
        return unit
    return math.nextafter(bound, math.inf if upward else -math.inf)


def _numeric(schema: dict, *, integral: bool, path: str):
    """A value inside the schema's bounds — both of them, and on the
    correct side of an exclusive one.

    The clamp this replaces only knew how to come DOWN to a bound
    (``if value > high: value = high``), so an exclusive ceiling was
    answered with the one number it forbids: ``{"exclusiveMaximum": 0}``
    gave ``0.0``, and ``exclusiveMinimum: 0`` with
    ``exclusiveMaximum: 0.5`` gave ``0.5`` (Codex P2). Exclusivity is
    not a detail of a bound, it IS the bound, and a fixture that lands
    on the endpoint is rejected by exactly the strict consumer keyless
    mode exists to keep honest.
    """
    low, low_open = _lower(schema)
    high, high_open = _upper(schema)

    if integral:
        # Nothing here converts to a float. Python's integers are
        # unbounded and exact, `math.floor` of one IS that integer, and
        # a 400-digit `minimum` is a perfectly good integer bound — it
        # answered correctly until a finiteness check written in terms
        # of `math.isfinite` started converting it and raising
        # OverflowError on the way (Codex P2, my own regression). Only a
        # FLOAT bound can be non-finite, so only a float is tested.
        for bound in (low, high):
            if isinstance(bound, float) and not math.isfinite(bound):
                raise Unsatisfiable(path)
        # An integer's neighbours are one step away, so an open bound is
        # just the next integer inside it. Non-integral bounds work the
        # same way: the smallest integer above 0.5 is 1 either way.
        floor = None if low is None else (
            math.floor(low) + 1 if low_open else math.ceil(low)
        )
        ceiling = None if high is None else (
            math.ceil(high) - 1 if high_open else math.floor(high)
        )
        if floor is not None and ceiling is not None and floor > ceiling:
            raise Unsatisfiable(path)
        if floor is not None:
            return int(floor)
        if ceiling is not None:
            # No floor: 0 unless the ceiling is below it.
            return int(min(0, ceiling))
        return 0

    lo = None if low is None else _to_float(low)
    hi = None if high is None else _to_float(high)
    if (lo is not None and not math.isfinite(lo)) or (
        hi is not None and not math.isfinite(hi)
    ):
        # A non-finite bound, which arrives more easily than it looks:
        # `json.loads` turns `1e999` into `inf` and accepts a bare
        # `NaN`, and an integer past the float range means an infinity
        # to a `number`. Left alone, these reached `math.floor(inf)` —
        # an OverflowError, a 500 where a refusal belongs — or came back
        # as the literal `NaN`, which is not JSON.
        #
        # Refusing all of them is deliberately blunter than the
        # arithmetic requires: `maximum: inf` excludes nothing and is
        # perfectly satisfiable. But no author writes it on purpose, a
        # refusal names the path and can be acted on, and the
        # alternative is four sign-and-direction cases guarding a 500. A
        # false refusal on an absurd schema is the cheaper mistake.
        raise Unsatisfiable(path)

    if lo is None and hi is None:
        candidate = 0.0
    elif lo is None:
        # No floor: sit at the ceiling, or at 0 when the ceiling allows.
        candidate = min(0.0, _step_inside(hi, upward=False) if high_open else hi)
    elif hi is None:
        candidate = _step_inside(lo, upward=True) if low_open else lo
    elif not low_open:
        # `lo` clears the floor by definition; the postcondition below
        # decides whether it also clears the ceiling.
        candidate = lo
    else:
        candidate = (lo + hi) / 2.0
        if not lo < candidate < hi:
            # Adjacent representable floats, or `lo + hi` overflowing to
            # infinity. Stepping off the floor is the smaller claim and
            # often still lands inside.
            candidate = _step_inside(lo, upward=True)

    # ONE postcondition, over every branch above. The version this
    # replaces checked containment on the two-sided path alone, so the
    # one-sided paths returned `bound ± 1.0` unexamined — and at large
    # magnitudes that is the bound itself (Codex P2). A candidate is
    # returned because it was checked, not because of how it was built.
    inside = (
        math.isfinite(candidate)
        and (lo is None or (candidate > lo if low_open else candidate >= lo))
        and (hi is None or (candidate < hi if high_open else candidate <= hi))
    )
    if not inside:
        raise Unsatisfiable(path)
    return candidate


def _word(seed: str, salt: str) -> str:
    digest = hashlib.sha256(f"{seed}|{salt}".encode()).hexdigest()
    return digest[:8]


MAX_CANDIDATES = 32


def _holds(value, schema, budget, depth: int = 0) -> bool:
    """Does the value satisfy the schema INCLUDING its combinators?

    `_satisfies` judges the keywords it implements and treats the rest as
    unknown, which is right — being unable to check something is not the
    same as having checked it. But a combinator is not unknown: this
    module evaluates `oneOf`, `anyOf` and `allOf` everywhere else, and
    treating them as automatically satisfied when TESTING MEMBERSHIP let
    `{"anyOf":[{"oneOf":[{"const":1},{"const":1}]},{"const":3}]}` accept
    `1` against an option no value can satisfy, when `3` satisfies the
    schema (Codex P2).

    Every membership question in this module goes through here, and every
    one of them asks it of the ORIGINAL option rather than the option
    merged with its parent. The merged form is lossy in exactly the way
    that matters: merging `{"type":"string","minLength":2}` under a
    parent `{"type":"number"}` keeps the parent's type and yields
    `{"type":"number","minLength":2}`, so `0.0` "matched" an option it
    contradicts, and `{"type":"number","oneOf":[{"minimum":1},
    {"type":"string","minLength":2}]}` answered `0.0` — a value
    satisfying NEITHER alternative, while `1` satisfies one (Codex P2).
    The merged form is for BUILDING a candidate; the original is for
    judging it. The parent's own constraints are checked separately by
    the caller, so nothing is lost by asking the option alone.
    """
    if not isinstance(schema, dict):
        return True
    if depth > _MAX_DEPTH:
        raise SchemaTooLarge("$")
    _spend(budget, "$")
    if not _satisfies(value, schema, 0, budget):
        return False
    for name in ("oneOf", "anyOf"):
        options = [o for o in (schema.get(name) or []) if isinstance(o, dict)]
        if not options:
            continue
        matched = sum(
            1 for option in options if _holds(value, option, budget, depth + 1)
        )
        if matched != 1 if name == "oneOf" else matched < 1:
            return False
    branches = [b for b in (schema.get("allOf") or []) if isinstance(b, dict)]
    if branches and not all(
        _holds(value, branch, budget, depth + 1) for branch in branches
    ):
        return False
    # A container's subschemas are walked HERE, not left to `_satisfies`.
    # It recurses into `properties` and `items` too, but with itself, so
    # a combinator one level inside a container went unevaluated exactly
    # as it did at the top before `_holds` existed: `{"anyOf":[{"type":
    # "object","properties":{"x":{"oneOf":[{"const":1},{"const":1}]}},
    # "required":["x"]},{"const":3}]}` accepted `{"x":1}`, though that
    # inner `oneOf` is satisfiable by nothing, while `3` satisfies the
    # schema (Codex P2). Evaluating combinators at the top and then
    # delegating the recursion is the same omission one level down —
    # the fix has to travel with the walk.
    properties = schema.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict):
        for name, sub in properties.items():
            if name in value and isinstance(sub, dict):
                if not _holds(value[name], sub, budget, depth + 1):
                    return False
    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict):
        if not all(
            _holds(element, items, budget, depth + 1) for element in value
        ):
            return False
    return True


def _candidates(schema, *, seed, path, depth, budget, limit, seen):
    """Yield the values a schema could synthesise, expanding combinators.

    `instance` returns ONE value, which is all any caller normally
    wants. `oneOf` is the exception: it must find a value matching
    exactly one branch, so when a branch carries its own combinator the
    single value `instance` picks from it may be the wrong one of
    several available. `{"oneOf":[{"anyOf":[{"enum":[1]},{"enum":[2]}]},
    {"enum":[1]}]}` took `1` from the nested `anyOf`, found it matched
    both outer branches, and fell back to it anyway — while `2`, one
    step further into the same branch, matches exactly one (Codex P2).

    Bounded twice over: `limit` caps how many values the whole search
    builds, and every expansion is charged to the shared budget, so a
    deeply nested combinator refuses rather than enumerating a tree.

    `seen` is the search's record of what it has already offered, shared
    across arms, seeds and neighbours because the consumer's verdict is
    the same for all three. Every counter here is charged through
    `_claim`, so none of them measures a repeat.
    """
    if limit[0] <= 0 or depth > _MAX_DEPTH or not isinstance(schema, dict):
        return
    for name in ("oneOf", "anyOf"):
        options = [o for o in (schema.get(name) or []) if isinstance(o, dict)]
        if not options:
            continue
        rest = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf")}
        merged = []
        for option in options:
            _spend(budget, path)
            try:
                merged.append(_merged([option], rest, budget))
            except Unsatisfiable:
                merged.append(None)
        for branch in merged:
            if branch is None or limit[0] <= 0:
                continue
            # No exclusivity filter here. One round ago there was one,
            # because `_satisfies` could not see a nested `oneOf` and a
            # candidate from an impossible inner branch reached the top
            # unchallenged. `_holds` sees it now, at every consumer of
            # this generator, so the filter became unreachable — and the
            # audit said so: injecting its removal stopped failing any
            # test. That is the question this batch keeps asking, and
            # here the answer was "the code is inert" rather than "the
            # case is missing" — removing it entirely changes no
            # behaviour and no test. Code an injection cannot reach is
            # not defence in depth, it is a second thing to keep true.
            yield from _candidates(
                branch, seed=seed, path=path, depth=depth + 1,
                budget=budget, limit=limit, seen=seen,
            )
        # The SIBLING supplies candidates too. `rest` drops both keys, so
        # the arms of the expanded combinator were the only source of
        # values one level down: a nested sibling could veto every
        # candidate through the consumer's `_holds` and never offer one.
        # `{"oneOf":[{"oneOf":[{"type":"integer"},{"type":"string"}],
        # "anyOf":[{"const":2}]},{"type":"integer","enum":["x"]}]}` fell
        # back to `0`, which matches neither outer arm, though `2`
        # satisfies the whole schema (Codex P2). Judging was never the
        # gap; supplying was.
        #
        # `instance` has TWO mechanisms for this and they are not
        # interchangeable. `_branch` merges the sibling INTO each arm;
        # `_sibling_seeds` draws candidates FROM the sibling's own
        # options. Porting the merge here is what the shape of the
        # finding suggests and it is the wrong one: this function
        # recurses, so the sibling would be re-merged at every level, and
        # an arm's 32-value enum intersected against a 180,000-value
        # sibling enum turned schemas that answer today into
        # `SchemaTooLarge` — decision 90's over-eager refusal, and
        # measured, not imagined: six tests went red. Seeds cost the
        # sibling's own options and nothing quadratic.
        # With their OWN allowance, for the reason decision 105(a) gave
        # the sibling seeds one level up: the arms run first and a value
        # the sibling forbids is still a value the arms produced, so on a
        # shared counter they spend the whole cap before the sibling gets
        # a turn. Measured at the boundary — 32 arms answered `31` and 96
        # answered `0`, which satisfies neither outer arm, for the same
        # schema shape. A cap meant to bound the search was deciding
        # which half of it ran (decision 111).
        other = "anyOf" if name == "oneOf" else "oneOf"
        seed_limit = [MAX_CANDIDATES]
        # Bounded refunds, for the reason the consuming search has them:
        # a seed the filter below rejects has told the search nothing and
        # must not spend the capacity meant for one that has. The filter
        # was added last round to stop a seed no arm accepts becoming the
        # answer, and it left exactly the starvation decision 111 is
        # about — 31 rejected seeds answered correctly and 32 returned
        # the fallback, the boundary sitting on `MAX_CANDIDATES` itself.
        # Bounded, because round 45 measured what unbounded refunds do:
        # they turn a search into a scan and break the schemas the cap
        # exists to protect.
        seed_refunds = [MAX_CANDIDATES]
        for option in (schema.get(other) or []):
            if seed_limit[0] <= 0:
                break
            if not isinstance(option, dict):
                continue
            # On a COPY of the allowance, committed only on success —
            # `_sibling_seeds` says why, and says it about a site that
            # had to be fixed after the pattern was already settled at
            # another: "a speculative probe must not be able to bankrupt
            # the search that follows it". A failed seed merge has done
            # its enum intersections; charging them leaves the real
            # budget short and the sources after this one find nothing.
            # This is the fourth site to take the same pattern.
            probe = _probe(budget)
            allowed = probe[0]
            try:
                seeded = _merged([option], rest, probe)
            except Unsatisfiable:
                # One unusable seed is not an unusable schema — the same
                # distinction decision 113(a) drew for `_distributed`.
                continue
            except SchemaTooLarge:
                # Too big to BUILD is survivable; out of value work is
                # not, and since round 61 both arrive here as the same
                # exception.
                if _value_exhausted(budget):
                    raise
                continue
            _settle(budget, probe, allowed)
            # Filtered against the WHOLE schema on the way out, which the
            # arms' candidates are not: an arm's value is a value the
            # schema asked for, while a seed is a value the sibling
            # merely permits. The invariant, stated so it can be checked
            # (decision 107): the sibling ADDS to the arms' search and
            # never stands in for it, so a seed no arm can accept must
            # not become the answer. Unfiltered, it replaced a refusal
            # with a plausible-looking wrong value — `{"oneOf":[{"oneOf":
            # [{"type":"integer","enum":["x"]},{"type":"integer","enum":
            # ["x"]}],"anyOf":[{"const":2}]},{"type":"integer","enum":
            # ["x"]}]}` has no satisfying value at all and began
            # answering `2`. Decision 108 puts that outside the defect
            # bar, an unsatisfiable schema answered at all being policy,
            # but the refusal is the better answer and this is what it
            # costs to keep.
            #
            # `_merged` returning a branch is NOT evidence the arm is
            # satisfiable — it leaves `{"type":"integer","enum":["x"]}`
            # exactly as it found it — so the weaker guard this replaces,
            # "seed only if some arm merged", could not see that case.
            for value in _candidates(
                seeded, seed=seed, path=path, depth=depth + 1,
                budget=budget, limit=seed_limit, seen=seen,
            ):
                if _holds(value, schema, budget):
                    yield value
                    continue
                # Dropped here, so it was never offered: the capacity
                # comes back, and so does the claim. The refund has been
                # bounded since round 50 and the release is not, because
                # they are not the same kind of thing — spending is what
                # a scan does too much of, while a claim the search did
                # not earn is simply wrong, and there is no ceiling below
                # which it stops being wrong. What bounds this loop is
                # the capacity and the node budget, as before.
                _release(value, seen, budget)
                if seed_refunds[0] > 0:
                    seed_refunds[0] -= 1
                    seed_limit[0] += 1
        return
    # An `enum` leaf offers every permitted value, not just the one
    # `instance` picks. `{"oneOf":[{"enum":[1,2]},{"enum":[1]}]}` had
    # both leaves produce `1`, so nothing in the search matched exactly
    # one branch, though `2` does (Codex P2). `const` is a one-element
    # enum for this purpose, as everywhere else in this module.
    permitted = _allowed_values(schema)
    if permitted is not None:
        for value in permitted:
            if limit[0] <= 0:
                return
            # `_holds`, not `_satisfies`. Round 43 moved the loop that
            # picks the value to RETURN and left this one, which decides
            # which values are OFFERED, so a member matching two inner
            # `oneOf` branches was still admitted (Codex P2).
            #
            # And the limit is charged on YIELD, not on examination. It
            # was decremented for every member looked at, so 32 copies
            # of an invalid member exhausted the allowance before a
            # valid 33rd was reached: with the cap at 32, 31 copies
            # answered correctly and 32 did not. A candidate that is
            # rejected is not a candidate, and must not spend the
            # capacity meant for one — the same starvation decision
            # 105(a) fixed for the sibling seeds, in the one place that
            # decides what a candidate IS.
            #
            # Scanning is still bounded: `_holds` charges the node
            # budget per call, so a long enum of rejects runs out of
            # allowance rather than running forever.
            # And not a member this search has ALREADY offered. The
            # limit was charged per copy, so 64 copies of a member the
            # consumer rejects exhausted the allowance and its refunds
            # before a distinct 65th was reached, and
            # `{"oneOf":[{"enum":[1…64 copies…,2]},{"const":1}]}`
            # answered `1`, which matches both arms, though `2` matches
            # one. Charged on yield since round 43 and refunded since
            # round 39; neither helps when every charge is for the same
            # value.
            if _holds(value, schema, budget) and _claim(value, seen, budget):
                limit[0] -= 1
                yield value
        return
    # Charged AFTER synthesis succeeds, for the same reason the enum
    # leaf above is charged on yield — and this is the enum leaf's own
    # sibling, three lines down, fixed one round later than it should
    # have been. A leaf that cannot be built is not a candidate: 32
    # contradictory `anyOf` arms spent the whole allowance without
    # yielding anything and starved a viable `{"const":2}` behind them,
    # so `{"oneOf":[{"anyOf":[…32 impossible…,{"const":2}]},
    # {"const":1}]}` raised `Unsatisfiable` though both `1` and `2` are
    # valid outer choices (Codex P2). The threshold sat exactly at the
    # cap again: 31 arms answered and 32 did not.
    try:
        value = instance(schema, seed=seed, path=path, depth=depth, budget=budget)
    except Unsatisfiable:
        return
    # The same claim the enum leaf makes, for the arms above it: 64
    # branches that each synthesise `1` charged the allowance 64 times
    # for one candidate and starved the arm holding `2`. The walk below
    # is NOT inside this — a repeated leaf value says nothing about its
    # neighbours, and they are what the walk is for.
    if _claim(value, seen, budget):
        limit[0] -= 1
        yield value
    # ONE lookup for EVERY kind of alternate, and no longer guarded on
    # the value's Python kind: round 64 added a third kind whose leaf can
    # be any shape at all. Two `setdefault` pairs meant an audit anchor
    # on them could not say which branch it had injected, and a case that
    # cannot name its own site proves nothing — the script says so about
    # itself, in the comment above `_inject_py`.
    state = _value_state(budget)
    neighbour_limit = state.setdefault("neighbour", [MAX_CANDIDATES])
    neighbour_refunds = state.setdefault("neighbour_refunds", [MAX_CANDIDATES])
    discarded = state.setdefault(
        "discarded_members", [_MAX_DISCARDED_MEMBER_PROBES]
    )
    # A leaf whose `type` is a UNION offers a value of EACH member, for
    # the same reason an enum leaf offers each member and a boolean leaf
    # offers both: the union IS the choice, and `instance` commits to one
    # of them and answers to itself rather than to the caller.
    # `{"oneOf":[{"type":["integer","array"]},{"type":"integer"}]}`
    # answered `0`, which matches both arms, though `[]` matches exactly
    # the union arm (Codex P2). Every union-beside-subset pair has the
    # same shape — string/number, boolean/null, object/array — and all
    # four were wrong.
    #
    # Round 63 did not cause this; it removed the accident that hid one
    # instance of it. Dropping an impossible product also dropped the
    # array that product happened to build, and that array was the only
    # value reaching the union arm. The report named the symptom and
    # asked for the cause — "ensure the remaining arms can contribute
    # alternatives from every declared union type" — and restoring the
    # accident would have left the other four shapes wrong, including
    # ones with no impossible arm anywhere in them.
    #
    # Finite by construction: JSON Schema has seven type names, so unlike
    # the numeric ring this can never walk and needs no per-leaf step
    # bound. The shared allowance and its bounded refund are the whole
    # accounting, exactly as decision 133(a) settled for the boolean
    # alternate — no new capacity, so no starvation guard is loosened.
    declared = schema.get("type")
    if isinstance(declared, list):
        members = _union_members(declared)
        for name in members:
            if neighbour_limit[0] <= 0:
                break
            # On a PROBE budget, the fifth site to take this pattern and
            # the first to have needed telling twice. A member is
            # OPTIONAL — the union offers it, the schema does not require
            # it — so a member too big to build must cost this search
            # nothing. Built against the live budget it raised
            # `SchemaTooLarge` out of `_candidates` and bankrupted a
            # request that had a cheap witness one arm away:
            # `{"oneOf":[{"type":["integer","array"],"minItems":10000},
            # {"const":0}]}` answered `1` before and refused after
            # (Codex P2).
            #
            # Decision 131, and I am exactly the caller it describes: a
            # new consumer of `instance` that caught the survivable
            # refusal and let the other one through. The two are split
            # here the way the other four sites split them — out of
            # nodes on a discardable probe is survivable, out of the
            # shared value allowance is not.
            # A DISCARDED probe is free, and free is only safe while
            # the NUMBER of them is bounded. Deduplicating the member
            # names bounds the repeats WITHIN this leaf; nothing bounded
            # them ACROSS leaves, so an arm count of 60 each offering its
            # own oversized member cost a second of event loop for a
            # schema whose answer is `0.5`. Charged on FAILURE only:
            # a member that built committed its nodes two lines below
            # and is already paid for.
            #
            # Exhausting that allowance NARROWS the probe; it does not
            # stop offering members. A `break` here meant eight oversized
            # arms in front of `{"type":["integer","boolean"]}` suppressed
            # the `true` that satisfies exactly that arm, and the search
            # answered an invalid `1` — the bound deciding an ANSWER
            # rather than a cost (Codex P2). Measured at the counter, the
            # threshold sat exactly on it: seven arms answered `true` and
            # eight did not.
            #
            # Sixty-four nodes, measured before the number was chosen:
            # every scalar member builds in 1 and the small containers in
            # 4, so a cheap member is still offered, while an array of
            # ten thousand fails after 64 instead of 10,001 — the cost of
            # a post-allowance failure falls by more than a hundredfold
            # rather than the member being skipped unseen.
            probe = _probe(budget)
            if discarded[0] <= 0:
                probe[0] = min(probe[0], _MAX_CHEAP_MEMBER_NODES)
            # What the probe was ALLOWED, read after any narrowing,
            # because that is what it was actually given and what its
            # remainder has to be measured against.
            allowed = probe[0]
            try:
                other = instance(
                    {**schema, "type": name}, seed=seed, path=path,
                    depth=depth + 1, budget=probe,
                )
            except Unsatisfiable:
                # Cheap: nothing was built, so nothing was discarded.
                continue
            except SchemaTooLarge:
                if _value_exhausted(budget):
                    raise
                if discarded[0] > 0:
                    discarded[0] -= 1
                continue
            # The probe isolates the FAILURE, not the cost of success:
            # a member that built is work this search really did, so its
            # nodes are committed before the value is judged — the SPEND,
            # not the remainder, which is the whole of `_settle`.
            _settle(budget, probe, allowed)
            # Claimed before it is charged, like every other candidate —
            # the member `instance` already committed to comes back here
            # first and must not spend the allowance twice for one value.
            if not _holds(other, schema, budget) or not _claim(
                other, seen, budget
            ):
                continue
            neighbour_limit[0] -= 1
            yield other
            # Refunded WITHOUT drawing on the shared refund cap, and that
            # is the one place this alternate is NOT like a numeric
            # neighbour. The cap exists to stop an UNBOUNDED generator
            # charging and refunding forever; this loop runs over a
            # declared list of at most seven type names, so there is no
            # unbounded loop for a cap to bound. Drawing on it anyway
            # made a union leaf spend capacity permanently once the cap
            # ran out, and 56 union arms emptied the counter the numeric
            # leaf behind them needed: the exclusive witness `0.5` became
            # an invalid string (Codex P2).
            #
            # Decision 126 in its MIRROR form. I checked that the union
            # alternate LOOSENED no starvation guard and never checked
            # that it COMPETED for the capacity those guards protect.
            # Net permanent spend is now 0 for a leaf whose members are
            # all rejected and 1 for one whose member is taken, which is
            # what every other candidate source costs.
            neighbour_limit[0] += 1
    # A numeric leaf offers a NON-INTEGRAL candidate too, because
    # exclusivity can turn on the integer/number boundary and nothing
    # `instance` returns ever sits on the non-integral side of it.
    #
    # `1.0` is an integer in JSON Schema — `_is_type` has always said so
    # — and that is the whole finding. For `{"oneOf":[{"type":"number"},
    # {"type":"integer"}]}` both leaves produce an integral value, every
    # integral value matches BOTH arms, and the exclusivity count
    # correctly rejects each one. The search was right and had nothing
    # left to consider, so the fallback returned `0.0`, which the schema
    # rejects, though `0.5` satisfies exactly the `number` arm (Codex
    # P2). This is the enum leaf's rule — offer the choices, not just the
    # one `instance` picks — reaching the one leaf whose choices are not
    # enumerable.
    #
    # Two offsets, not a search over the reals: half a unit either side
    # of the value already synthesised. `_holds` decides whether either
    # is admissible, so `{"type":"integer"}` offers nothing extra and
    # costs one rejected check, and a bound that excludes both leaves the
    # leaf exactly as it was. A schema whose only non-integral values lie
    # elsewhere — `{"type":"number","minimum":0,"exclusiveMaximum":0.25}`
    # beside an `integer` arm — is not reached by this, and is the stated
    # residual rather than a claim.
    #
    # On an allowance of its OWN, hung off the budget exactly as the
    # value-work allowance is, because that list is already the one
    # object shared by reference down the whole call tree and living
    # exactly as long as the request.
    #
    # Charging these to `limit` was the first thing I wrote and decision
    # 116 caught it in the same hour it was recorded: a numeric leaf then
    # spends two slots instead of one, so a `oneOf` of crowded numeric
    # arms reaches half as far, and schemas with 32 to 63 such arms went
    # from answering correctly to answering wrongly. **An over-eager
    # change that breaks what works today is the worse defect** (decision
    # 90), and this one was introduced by a fix for the opposite problem.
    # Measured at the counter's value: the boundary fell from 64 arms to
    # 32, which is `MAX_CANDIDATES` and its refunds exactly.
    #
    # Bounded across the whole search rather than per leaf, so the extra
    # breadth cannot grow with the arm count; and charged on YIELD, after
    # `_holds`, like everything else that decides what a candidate IS.
    # And a DIFFERENT INTEGER, for the same reason and by the same
    # mechanism. Round 51 taught the leaf to cross the integer/number
    # boundary and stopped there, so when the synthesised integer landed
    # in an overlapping branch the leaf had nothing left to offer:
    # `{"oneOf":[{"type":"integer"},{"const":0}]}` answered `0`, which
    # matches both arms, though `1` satisfies exactly the integer arm —
    # and `±0.5` cannot satisfy an `integer` arm at all (Codex P2).
    # Decision 109 in its third form this batch: the question is "what
    # neighbours can this leaf offer", and I had answered it for one kind
    # of neighbour and not the other.
    #
    # The fractional offsets come FIRST, so a schema whose answer is a
    # fraction still gets the fraction: `+1` is an integer to JSON Schema
    # even when the value is `0.0`, so leading with it would change
    # rounds 51 and 53 from `0.5` and `-0.5` to `1.0` and `-1.0`. Both
    # are valid, and pinning the ones already recorded is cheaper than
    # explaining a churned answer.
    #
    # The counter is named for what it holds. It bounded only fractional
    # candidates when it was written and now bounds both kinds, and a
    # name describing half of what a counter holds is decision 111 in the
    # naming dimension — the census test is what caught the rename.
    if isinstance(value, bool):
        # A boolean leaf has TWO values and offered one, so the rule that
        # reached enum leaves in round 43 and numeric leaves in round 54
        # never reached the third kind of leaf with a finite choice:
        # `{"oneOf":[{"type":"boolean"},{"const":true}]}` fell back to
        # `true`, which matches both arms, though `false` matches exactly
        # the boolean one (Codex P2). Decision 109 in its plainest form —
        # offer the choices, not only the one `instance` picked — and the
        # census for "which leaves have a choice" had two entries where
        # it should have had three.
        #
        # The same allowance and the same bounded refund as the numeric
        # alternates, because it is the same kind of candidate: one the
        # leaf offers beyond what `instance` built. One extra value, so
        # it can never walk; the accounting is shared so the census that
        # pairs every candidate counter with a refund still reads true.
        other = not value
        if neighbour_limit[0] > 0 and _holds(other, schema, budget):
            if _claim(other, seen, budget):
                neighbour_limit[0] -= 1
                yield other
                if neighbour_refunds[0] > 0:
                    neighbour_refunds[0] -= 1
                    neighbour_limit[0] += 1
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Bounded PER LEAF as well as across the request. The shared
        # allowance counts candidates that were yielded, and a leaf whose
        # neighbours are all rejected by the outer combinator gets each one
        # refunded — so an unbounded walk lets ONE leaf spend the whole
        # allowance and every later leaf finds nothing. Measured, not
        # imagined: `{"oneOf":[{"type":"integer"},{"type":"number"}]}`
        # began answering `0`, because the integer leaf walked its whole
        # ring and the number leaf never got to offer `0.5` — the round-51
        # answer, broken by the round-55 widening. Decision 111 again, and
        # the third counter to need the question "who pays, and for what".
        #
        # Eight steps covers the overlaps this is for — the reported case
        # needs two, the widest constructed needs three — and caps the
        # `_holds` calls a single leaf can charge to the node budget, which
        # is the other thing the unbounded walk broke.
        # TWO bounds, because they answer different questions and one
        # constant doing both was wrong at one of them — decision 111, in
        # code I wrote this round. `_MAX_NEIGHBOUR_STEPS` is how FAR the
        # walk goes; `_MAX_NEIGHBOUR_YIELDS` is how many candidates one
        # leaf may take from the shared allowance. Sharing a cap of 4 made
        # `{"oneOf":[{"type":"integer"},{"enum":[-2,-1,0,1,2]}]}` fail: the
        # answer `3` is four yields out, and the yield cap bit before the
        # distance cap it was standing in for.
        offered = 0
        for step in range(1, _MAX_NEIGHBOUR_STEPS + 1):
            admissible = False
            for offset in (step - 0.5, -(step - 0.5), step, -step):
                if neighbour_limit[0] <= 0 or offered >= _MAX_NEIGHBOUR_YIELDS:
                    return
                if isinstance(value, int) and not isinstance(offset, int):
                    # BEFORE the addition, because the addition is what
                    # raises. Python ints are unbounded and floats are
                    # not, so `10**400 + 0.5` is an `OverflowError` — a
                    # gateway 500 for a schema whose answer is simply
                    # `10**400 + 1` (Codex P2). Round 56 already decided
                    # an int leaf has no use for a fractional neighbour;
                    # it checked after computing one, which works up to
                    # the float range and crashes past it.
                    #
                    # A half step from a huge integer is not
                    # representable, so there is nothing to skip TO, and
                    # the integral offsets in this same ring do the work.
                    continue
                neighbour = value + offset
                if neighbour == value:
                    # The offset vanished into the float's own precision:
                    # `1e20 + 0.5 == 1e20`, so every neighbour the walk
                    # proposed WAS the value it started from, and a schema
                    # whose answer is one representable step away got the
                    # forbidden endpoint back (Codex P2).
                    #
                    # `_step_inside` already knows this — it was written
                    # for the same arithmetic at a BOUND, where `bound +
                    # 1.0` rounded back and answered with the endpoint an
                    # `exclusiveMinimum` forbids. Decision 109 for the
                    # fourth time in this batch, and the first where the
                    # other place that answers the question was already
                    # correct and one screenful away: the census is not
                    # only for code you are adding.
                    # Only a float leaf reaches here: the fractional
                    # offsets an int leaf cannot use were skipped above,
                    # and an int plus an int offset never equals itself.
                    neighbour = value
                    for _ in range(step):
                        neighbour = _step_inside(neighbour, upward=offset > 0)
                if _holds(neighbour, schema, budget):
                    # `admissible` first and unconditionally, because it
                    # answers a different question: whether this ring is
                    # worth walking past, which a repeat answers just as
                    # well as a novelty. Only the CHARGE and the yield
                    # are withheld below, so the walk's extent is exactly
                    # what it was.
                    admissible = True
                    if not _claim(neighbour, seen, budget):
                        # Past the float range of the fixed offsets,
                        # EVERY offset in a ring vanishes — the ULP of
                        # `1e20` is 16384 — so `step - 0.5` and `step`
                        # both route through `_step_inside` and land on
                        # the same representable neighbour. Two yields,
                        # one candidate: the per-leaf cap measured
                        # attempts and the walk stopped at the fourth
                        # step instead of the eighth. With `1e20` and its
                        # first four upward floats occupied,
                        # `{"oneOf":[{"type":"number","minimum":1e20},
                        # {"enum":occupied}]}` fell back to `1e20`, which
                        # the schema rejects, though the fifth upward
                        # float satisfies exactly one arm (Codex P2).
                        continue
                    neighbour_limit[0] -= 1
                    offered += 1
                    yield neighbour
                    # Resuming means the consumer came back for another
                    # candidate, which is the only thing it does when it has
                    # REJECTED this one — an accepted candidate ends the
                    # search and this generator is closed at the `yield`
                    # instead. So the refund goes here, where the verdict is
                    # finally knowable, and `GeneratorExit` skips it exactly
                    # when it should.
                    #
                    # `_holds` above asks whether the fraction satisfies THIS
                    # leaf; the consumer asks whether it satisfies the outer
                    # combinator, and the allowance was spent before that
                    # second question was put. Locally valid fractions
                    # rejected for matching another arm emptied it, and the
                    # one exclusive fraction behind them was never offered
                    # (Codex P2). The boundary sat on `MAX_CANDIDATES` for
                    # the third round running.
                    #
                    # `limit` has had bounded refunds since round 39 and
                    # `seed_limit` since round 50; this counter had none. A
                    # census of the file said so in one line — `refunds` six
                    # occurrences, `seed_refunds` five, `neighbour_refunds`
                    # zero — and that is the check decision 109 asks for,
                    # applied to counters rather than to shapes.
                    if neighbour_refunds[0] > 0:
                        neighbour_refunds[0] -= 1
                        neighbour_limit[0] += 1
            if not admissible:
                # Every offset in this ring was refused by the leaf itself,
                # so the ones beyond it will be too — a bounded schema does
                # not become satisfiable further from its own bounds. This
                # is what keeps the walk from charging the node budget for
                # a leaf it can never help: without it a `oneOf` of 63
                # narrow numeric bands spent enough on refused `_holds`
                # calls to turn a schema that ANSWERED into
                # `SchemaTooLarge` — decision 117's narrowing direction,
                # caught by a test from three rounds ago.
                return
                    # And then the OTHER offset, rather than ending the leaf.
                    # `+0.5` and `-0.5` are two candidates, not one with a
                    # spare: the refund last round handed the capacity back
                    # and an unconditional `return` immediately threw it
                    # away. `{"oneOf":[{"type":"number"},{"type":"number",
                    # "minimum":0}]}` has `0.0` and `+0.5` matching both
                    # arms, so the answer is `-0.5` — which was never
                    # offered (Codex P2). Refunding a candidate and then
                    # declining to spend the refund is a fix that stops one
                    # line short of working.


def instance(schema, *, seed: str = "", path: str = "$", depth: int = 0, budget=None):
    """A value satisfying ``schema``. Unknown or absent types become a
    marked string, which is the shape most agents ask for.

    ``budget`` is the total-node allowance, created at the top call and
    shared by every descendant. Raises ``SchemaTooLarge`` when a schema
    asks for more than a fixture — see ``MAX_NODES``.
    """
    if budget is None:
        budget = [MAX_NODES]
    budget[0] -= 1
    if budget[0] < 0:
        raise SchemaTooLarge(path)
    if depth > _MAX_DEPTH or not isinstance(schema, dict):
        return f"{STUB_MARKER} {_word(seed, path)}"

    # `allOf` is not `anyOf`. The result must satisfy EVERY subschema,
    # and taking the first one produced an object with `a` for a schema
    # requiring `a` and `b` — rejected by a strict consumer although the
    # schema was perfectly satisfiable (Codex P2).
    branches = schema.get("allOf")
    if isinstance(branches, list) and branches:
        return instance(
            _merged(branches, schema, budget),
            seed=seed,
            path=f"{path}.allOf",
            depth=depth + 1,
            budget=budget,
        )

    # `oneOf`/`anyOf` genuinely are a choice — but a choice made INSIDE
    # the parent schema, not instead of it. Delegating to `options[0]`
    # alone threw the parent away, so
    # `{"type":"integer","minimum":10,"anyOf":[{"maximum":20}]}` came
    # back as a marked string: the chosen branch declares no type, and
    # the type it had to respect was one level up (Codex P2).
    for combinator in ("oneOf", "anyOf"):
        options = [o for o in (schema.get(combinator) or []) if isinstance(o, dict)]
        if not options:
            continue
        rest = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf")}
        # Everything except the combinator being searched — including a
        # SIBLING combinator, which `rest` drops. The loop takes `oneOf`
        # first and returns from inside it, so a sibling `anyOf` was
        # stripped and never enforced: `{"oneOf":[{"const":1},
        # {"const":2}],"anyOf":[{"const":2}]}` answered `1`, violating
        # the `anyOf`, though `2` satisfies the whole schema (Codex P2).
        # `rest` still does the BUILDING — carrying the sibling into
        # every branch merge would change what is synthesised — while
        # this does the judging, through `_holds`, which evaluates it.
        siblings = {k: v for k, v in schema.items() if k != combinator}

        # Merged one at a time, under the CALLER's budget. Two faults
        # lived in the eager `[_merged([option], rest) for option in
        # options]` this replaces (Codex P1). Each merge took a fresh
        # allowance, so 2,000 disjoint options against a 9,000-member
        # parent enum bought 2,000 x 9,000 comparisons and blocked for
        # seconds on an ~11,000-node schema. And merging every option up
        # front pays for all of them even when the first one works —
        # which, once the budget is shared, would spend the whole
        # allowance before the search begins and turn schemas that
        # answer today into refusals. Sharing the budget REQUIRES the
        # laziness; the two halves are one fix.
        # The sibling combinator GENERATES as well as judges. Last round
        # taught `siblings` to reject a candidate the sibling forbids;
        # that is only half of it, because the arms alone may not offer
        # a value the sibling permits. `{"oneOf":[{"type":"integer"},
        # {"type":"string"}],"anyOf":[{"const":2}]}` produced `0` and a
        # string, both correctly rejected, and then fell back to `0` —
        # while `2` satisfies the whole schema (Codex P2). Merging the
        # sibling in as another `allOf` branch puts its options into
        # `_merged`'s group collection, so the branch carries them and
        # `_candidates` expands them: `{"type":"integer"}` with
        # `{"const":2}` yields `2`.
        other = "anyOf" if combinator == "oneOf" else "oneOf"
        sibling = [o for o in (schema.get(other) or []) if isinstance(o, dict)]
        with_sibling = [{other: sibling}] if sibling else []

        def _branch(option, _rest=rest, _extra=with_sibling):
            # The sibling is an ADDITION to the search, never a
            # subtraction from it. An arm carrying its own combinator
            # plus the sibling's is two groups, and `_merged` refuses a
            # multi-group shape containing a `oneOf` by name (decision
            # 95) — so merging the sibling in unconditionally turned
            # `{"oneOf":[{"oneOf":[{"const":1},{"const":2}]},
            # {"const":3}],"anyOf":[{"const":2}]}` from `2` into a
            # refusal. **A regression of my own making, and precisely
            # the failure this module treats as the worse one: an
            # over-eager refusal breaks a schema that worked** (Codex
            # P2, decision 90).
            #
            # When the combination cannot be built, the arm is built
            # alone. Nothing is lost: `siblings` still JUDGES every
            # candidate, so a value the sibling forbids is still
            # rejected — the sibling simply stops contributing
            # candidates for that one arm, which is where this stood
            # before the previous round. The retry can only widen what
            # the search considers, never narrow it, and an arm that is
            # impossible on its own raises here exactly as it did.
            if _extra:
                # Spent against a COPY, committed only if it works. The
                # merge that ends in `Unsatisfiable` still does all the
                # enum intersections first, and that spend used to come
                # out of the shared allowance and then be thrown away —
                # so the fallback repeated the same arm work on a
                # depleted budget and a satisfiable schema came back
                # `SchemaTooLarge`. Measured: two 2,000-member enums
                # answered `1` without the sibling and refused with it
                # (Codex P2). Another over-eager refusal introduced by
                # last round's widening, one step further along.
                #
                # `SchemaTooLarge` is caught here, unlike everywhere
                # else in this module, and only because the probe's
                # spend is discarded: the real budget is untouched, so
                # falling back cannot let a schema exceed its allowance.
                # The fallback then spends it exactly as it would have
                # had the speculative merge never run.
                probe = _probe(budget)
                allowed = probe[0]
                try:
                    merged = _merged([option] + _extra, _rest, probe)
                except Unsatisfiable:
                    merged = None
                except SchemaTooLarge:
                    if _value_exhausted(budget):
                        raise
                    merged = None
                if merged is not None:
                    _settle(budget, probe, allowed)
                    return merged
            return _merged([option], _rest, budget)

        def _sibling_seeds(_rest=rest, _sib=sibling):
            """Candidate sources drawn from the SIBLING's own options.

            When an arm cannot take the sibling in its merge, the
            sibling can neither build nor veto its way to an answer: the
            arm offers values the sibling forbids, the check rejects
            every one, and the fallback returns a rejected value anyway.
            `{"oneOf":[{"oneOf":[{"type":"integer","minimum":2},
            {"type":"integer","maximum":0}]}],"anyOf":[{"const":5}]}`
            answered `2` that way, though `5` satisfies the arm's first
            alternative AND the sibling (Codex P2).

            The sibling's options are a source of candidates in their own
            right. Anything they produce still faces every check the arm
            candidates face — the exclusivity count and `siblings` — so
            this widens where values come from without widening what is
            accepted.

            The merge runs on a COPY of the allowance and commits its
            spend only when it succeeds — `_branch` above does exactly
            this and this generator was left on the live budget. A seed
            merge that failed had already spent, so `SchemaTooLarge`
            was caught here with the shared budget driven NEGATIVE, and
            the generator then ended quietly: every later source found
            nothing left and the fallback was returned. A parent enum
            `0..5999`, an arm `{"oneOf":[{"type":"integer"}]}` and a
            sibling `anyOf` enum `5000..10999` answered `0`, which the
            sibling rejects, though `5000` satisfies the whole schema
            (Codex P2).

            A speculative probe must not be able to bankrupt the search
            that follows it. This is the third pattern in this function
            that had to be applied to a second site after being settled
            at the first.
            """
            for option in _sib:
                probe = _probe(budget)
                allowed = probe[0]
                try:
                    seeded = _merged([option], _rest, probe)
                except Unsatisfiable:
                    continue
                except SchemaTooLarge:
                    if _value_exhausted(budget):
                        raise
                    continue
                _settle(budget, probe, allowed)
                yield seeded

        if combinator == "anyOf":
            # The FIRST option that actually works, not simply the first
            # option. A parent constraint can contradict one branch and
            # not another — `{"type":"integer","anyOf":[{"type":"string"},
            # {"minimum":5}]}` merged the parent over branch 0 and
            # synthesised `0`, which satisfies neither alternative,
            # although branch 1 yields a perfectly good `5` (Codex P2).
            fallback = None
            for option in options:
                try:
                    candidate = instance(
                        _branch(option), seed=seed, path=f"{path}.{combinator}",
                        depth=depth + 1, budget=budget,
                    )
                except Unsatisfiable:
                    # A contradictory option is not a contradictory
                    # schema. `{"type":"integer","anyOf":[{"type":
                    # "string","enum":["x"]},{"minimum":5}]}` raised on
                    # branch 0 and never reached branch 1, which yields
                    # `5` (Codex P2). The search is the point; a branch
                    # that cannot be built is a branch to skip.
                    #
                    # `SchemaTooLarge` is deliberately NOT caught: that
                    # is a statement about the whole request's budget,
                    # and swallowing it per branch would let a schema
                    # spend the allowance once per option.
                    continue
                if fallback is None:
                    fallback = candidate
                # The ORIGINAL options, and `_holds` rather than
                # `_satisfies`: the merged form has had the parent's
                # constraints folded into it and a nested combinator
                # would go unevaluated. Both are checked — the parent
                # here, one option there — so nothing is lost by asking
                # them separately.
                if _holds(candidate, siblings, budget) and any(
                    _holds(candidate, option, budget) for option in options
                ):
                    return candidate
            # None could be SEEN to satisfy an option. As with `oneOf`
            # below, the first branch's value stands rather than a
            # refusal: `_satisfies` judges only what it implements, and
            # an over-eager check is the worse failure (decision 90).
            if fallback is None:
                # Every option raised, so there is nothing to stand on.
                raise Unsatisfiable(f"{path}.{combinator}")
            return fallback
        # No shortcut for a single option. There was one — `instance`
        # straight off `_branch(options[0])` — and it skipped every check
        # the loop below does: the exclusivity count, and (since last
        # round) the sibling. With the retry dropping the sibling from
        # the BUILD for an arm that cannot take it, the singleton path
        # enforced it in neither place, so `{"oneOf":[{"oneOf":[{"type":
        # "integer","minimum":2},{"type":"integer","maximum":0}]}],
        # "anyOf":[{"const":5}]}` answered `2` where only `5` works
        # (Codex P2). A fast path that skips the checks is not a fast
        # path, it is a second implementation with fewer of them — and
        # the loop handles one option correctly by construction.
        # `oneOf` means EXACTLY one, so a value satisfying two branches
        # is rejected by a strict validator however well it fits the
        # first: `{"oneOf":[{"type":"integer"},{"minimum":0}]}` gave `0`,
        # which satisfies both (Codex P2). Each branch is tried and the
        # first candidate that clears exactly one is taken.
        fallback = None
        # Every value a branch could produce, not the first one it
        # happens to produce — see `_candidates`. The cap is shared
        # across the whole search, so the work is bounded by the schema
        # rather than by the number of branches.
        limit = [MAX_CANDIDATES]
        # What the search has already offered, so no counter below
        # measures the same candidate twice — see `_claim`. One set for
        # the whole search rather than one per counter: the consumer
        # judges a candidate by the candidate alone, so an arm, a seed
        # and a neighbour that produce the same value are producing the
        # same verdict, and only the first of them is worth paying for.
        #
        # Two stores, the shape `_enum_index`, `_member` and `_intersect`
        # already use: a `_key` index for the values Python can hash, and
        # a list compared pairwise with `_same` for the lists and objects
        # it cannot.
        seen = (set(), [])
        # The refunds are themselves BOUNDED, and that bound is the
        # whole lesson of this round. Returning a slot on every
        # contextual rejection is right in principle — the candidate
        # told the search nothing — but done without a ceiling it stops
        # being a search and becomes a scan: the seed generator walked
        # an entire 90,000-member enum, spent the node budget, and
        # schemas that answered before began raising `SchemaTooLarge`.
        # That is decision 90 caused by the fix for decision 111(b),
        # caught by the audit's own green-baseline check rather than by
        # me, because my over-refusal probe used small schemas and the
        # regression only shows on large ones.
        #
        # So a search may recover from a bounded run of unusable
        # candidates and may not sweep an unbounded one. Two caps, and
        # the node budget underneath both.
        refunds = [MAX_CANDIDATES]
        seed_refunds = [MAX_CANDIDATES]
        # A SEPARATE allowance for the sibling seeds. They are drawn
        # after the arms and shared one cap with them, so an arm could
        # spend the whole thing on candidates the sibling rejects and
        # the seeds — the only source that could answer — never got to
        # speak: `{"oneOf":[{"oneOf":[{"enum":[0…31,50]},{"const":0}]}],
        # "anyOf":[{"const":50}]}` answered `0`, violating the inner
        # exclusivity AND the sibling, while the seed `50` satisfies
        # everything (Codex P2). A newly added source that a older one
        # can starve is not a source.
        #
        # Two caps rather than an interleave, deliberately: interleaving
        # would have to materialise the seed branches to round-robin
        # them, and merging every sibling option up front is the eager
        # mistake decision 104 just fixed. The total stays bounded —
        # two fixed caps — and the real bound on work is the budget.
        seed_limit = [MAX_CANDIDATES]
        # Every option merged ONCE, and a branch whose merge is
        # impossible recorded as `None` rather than raising. Round 29
        # established that a contradictory option is not a contradictory
        # schema, with a `try` around the whole per-branch body — and
        # moving to `_candidates` last round left `_branch(option)`
        # outside it, so one impossible option aborted the entire search
        # again (Codex P2). A refactor is exactly how a fixed bug comes
        # back: the catch was still in the file, just no longer around
        # the call that raises. It escaped from a SECOND place too, the
        # exclusivity count below, which called `_branch(other)` for
        # every sibling of every candidate — unguarded, and quadratic.
        #
        # Unlike `anyOf`, these merges cannot be lazy: counting matches
        # requires every option, so the bound here is doing them once
        # rather than per candidate.
        merged = []
        for option in options:
            try:
                merged.append(_branch(option))
            except Unsatisfiable:
                merged.append(None)
        # Chained, not concatenated: `list(_sibling_seeds())` merged
        # every sibling option up front and spent the budget doing it,
        # so an arm that would have answered immediately was charged for
        # seeds nobody needed — and the 2,000-member case went straight
        # back to `SchemaTooLarge`. The same laziness decision 99 is
        # about, one generator further along.
        for branch, from_sibling in itertools.chain(
            ((b, False) for b in merged),
            ((b, True) for b in _sibling_seeds()),
        ):
            if branch is None:
                continue
            allowance = seed_limit if from_sibling else limit
            refundable = seed_refunds if from_sibling else refunds
            for candidate in _candidates(
                branch, seed=seed, path=f"{path}.{combinator}",
                depth=depth + 1, budget=budget,
                limit=allowance, seen=seen,
            ):
                if fallback is None:
                    fallback = candidate
                # Counted against the ORIGINAL options. Counting
                # against `merged` erased the very conflict that decides
                # membership: `{"type":"string","minLength":2}` under a
                # parent `{"type":"number"}` merges to `{"type":"number",
                # "minLength":2}`, so `0.0` counted as an exclusive match
                # for an option it contradicts (Codex P2). The merged
                # form builds; the original judges.
                matched = sum(
                    1 for other in options if _holds(candidate, other, budget)
                )
                if matched == 1 and _holds(candidate, siblings, budget):
                    return candidate
                # The capacity comes BACK. The leaf was satisfiable —
                # that is why it was offered — and what refused it was
                # the CONTEXT: the exclusivity count, or a sibling. A
                # candidate the consumer throws away has told the search
                # nothing and must not cost it the slot a later value
                # needs, or 32 leaf-valid-but-context-rejected values
                # starve an exclusive one behind them:
                # `{"oneOf":[{"enum":[0…31,50]},{"type":"integer",
                # "maximum":31}]}` answered the fallback `0`, which
                # matches BOTH branches, while `50` matches exactly one
                # (Codex P2).
                #
                # The real bound is the node budget, not this counter:
                # every `_holds` above charges it, so the loop runs out
                # of allowance long before it runs forever. The counter
                # bounds how many candidates a branch may OFFER, which
                # is what it was for.
                if refundable[0] > 0:
                    refundable[0] -= 1
                    allowance[0] += 1
        if fallback is None:
            raise Unsatisfiable(f"{path}.{combinator}")
        # No branch yielded a value this module can SEE satisfying
        # exactly one. `_satisfies` errs towards saying yes — it judges
        # only the keywords it implements — so the overlap may not be
        # real, and refusing here would turn a satisfiable schema into
        # an error, which decision 90 is about. The first branch's value
        # stands, and `oneOf` is listed as best-effort rather than
        # honoured for exactly this reason.
        return fallback
    if "$ref" in schema:
        # No resolver here on purpose: a stub that chased references
        # could loop, and an agent that uses $ref gets a marked string
        # rather than a hang.
        return f"{STUB_MARKER} {_word(seed, path)}"

    candidates = _allowed_values(schema)
    if candidates is not None:
        # The first permitted value that also clears the schema's other
        # constraints — not simply the first one listed. An empty list
        # gets here too: no value is permitted, either written that way
        # or left over from intersecting `allOf` branches that share
        # none, and the loop falls straight through to the refusal.
        for candidate in candidates:
            # `_holds`, not `_satisfies`. Decision 105 settled that every
            # membership question in this module is combinator-aware, and
            # this one — which picks WHICH permitted value to return —
            # was left behind. `_satisfies` walks into `properties` and
            # `items` but cannot see a combinator once there, so
            # `{"type":"object","enum":[{"x":1},{"x":2}],"properties":
            # {"x":{"oneOf":[{"const":1},{"const":1},{"const":2}]}},
            # "required":["x"]}` returned `{"x":1}` — matching two inner
            # branches, so invalid — while `{"x":2}` matches exactly one
            # (Codex P2). The array spelling was wrong the same way.
            if _holds(candidate, schema, budget):
                return candidate
        raise Unsatisfiable(path)
    if "default" in schema and _holds(schema["default"], schema, budget):
        # `default` is an ANNOTATION in JSON Schema and is not required
        # to satisfy the schema it sits in, so returning it unchecked
        # answered `{"type":"integer","default":"unknown"}` with a
        # string (Codex P2). It is a candidate like an enum member: used
        # when it fits, and otherwise synthesised the ordinary way.
        return schema["default"]

    declared = schema.get("type")
    if isinstance(declared, list):
        # A union is a CHOICE, and this committed to the first non-null
        # member without asking whether that member could satisfy the
        # rest of the schema. `{"type":["string","integer"],
        # "minLength":2,"maxLength":1}` came back as the one-character
        # string `"["` — the length keywords contradict each other for a
        # string and do not apply to an integer at all, so `0` satisfies
        # the whole schema (Codex P2).
        #
        # Each member is tried in turn, under its own `type`, and the
        # first candidate that satisfies the WHOLE schema wins. When
        # none does, the first member's value stands: the schema is then
        # contradictory whichever way it is read, and producing the
        # coherent thing beats inventing a refusal for the caller's own
        # impossible schema — the choice this module makes throughout.
        members = _union_members(declared)
        if len(members) > 1:
            first = None
            for name in members:
                try:
                    candidate = instance(
                        {**schema, "type": name}, seed=seed, path=path,
                        depth=depth + 1, budget=budget,
                    )
                except Unsatisfiable:
                    continue
                if first is None:
                    first = candidate
                if _holds(candidate, schema, budget):
                    return candidate
            if first is None:
                raise Unsatisfiable(path)
            return first
        declared = members[0] if members else None

    if declared is None and "properties" in schema:
        # INFERRED, not declared. `properties` and `required` constrain
        # object instances and do not require the instance to be one, so
        # a failure here is not the schema's failure — it is this
        # module's guess being wrong. Built under an explicit type so
        # the declared path below does the work, then judged; only if
        # that cannot satisfy does another type get a turn.
        try:
            built = instance(
                {**schema, "type": "object"},
                seed=seed, path=path, depth=depth + 1, budget=budget,
            )
        except Unsatisfiable:
            built = _NO_ALTERNATIVE
        if built is not _NO_ALTERNATIVE and _holds(built, schema, budget):
            return built
        alternative = _other_type(schema, budget, avoid=({},))
        if alternative is not _NO_ALTERNATIVE:
            return alternative
        # Nothing else fits either: the caller's schema is contradictory
        # however it is read, so the object stands — or its refusal does.
        if built is not _NO_ALTERNATIVE:
            return built
        raise Unsatisfiable(path)

    if declared == "object":
        out = {}
        properties = schema.get("properties") or {}
        required = [
            key
            for key in (schema.get("required") or [])
            if isinstance(key, str)
        ]
        # Every required key, whether or not `properties` describes it.
        # Filtering `required` THROUGH `properties` dropped the ones it
        # did not mention, so `{"type":"object","required":["x"]}` — a
        # valid and perfectly satisfiable schema — came back as `{}` and
        # failed its own validation (Codex P2). A required key with no
        # schema is unconstrained, not absent.
        wanted = required or list(properties)
        compulsory = set(required)
        for key in wanted:
            sub = properties.get(key) or {}
            try:
                value = instance(
                    sub, seed=seed, path=f"{path}.{key}",
                    depth=depth + 1, budget=budget,
                )
            except Unsatisfiable:
                # An OPTIONAL property that cannot be built is simply
                # left out; the object is still valid without it. Only a
                # required one takes the whole object down with it.
                if key in compulsory:
                    raise
                continue
            # And a property whose value does not satisfy its own
            # subschema is left out for the same reason. `{"type":
            # "object","properties":{"x":{"type":"string","minLength":2,
            # "maxLength":1}}}` is satisfiable as `{}` — `x` is not
            # required — but the stub emitted a one-character `x` and
            # returned a document the schema rejects (Codex P2). The
            # bounds themselves stay forgiving of the caller's own
            # contradiction, which is why the value gets built at all;
            # what changes is that an object does not have to carry it.
            # `_holds`, not `_satisfies`. Round 34 made every membership
            # question combinator-aware and round 35 made it walk into
            # containers — and these two sites, which decide whether an
            # optional member is fit to keep, were never moved over.
            # `{"type":"object","properties":{"x":{"oneOf":[{"const":1},
            # {"const":1}]}}}` kept `x` because `_satisfies` cannot see
            # that inner `oneOf`, though `{}` satisfies the schema
            # (Codex P2). Same walk, same judge, everywhere.
            if key not in compulsory and isinstance(sub, dict) and not _holds(
                value, sub, budget
            ):
                continue
            out[key] = value
        return out
    if declared == "array":
        items = schema.get("items") or {}
        wanted = schema.get("minItems")
        # Only an int is a length — see `_is_length`. A string or a float
        # here is a malformed schema, and `int("1e9")` would be a
        # ValueError escaping as a 500 rather than a refusal.
        count = max(wanted, 1) if _is_length(wanted) else 1
        maximum = schema.get("maxItems")
        if _is_length(maximum):
            count = min(count, max(maximum, 0))
        # No separate pre-check on `count`. I wrote one — "refuse before
        # the comprehension rather than during it" — and injecting its
        # removal changed no test, because `range` is lazy and the
        # per-node budget stops the comprehension after MAX_NODES items
        # either way. One bound, not a bound and a flourish (the same
        # finding as decision 73's `close()`).
        built = []
        for i in range(count):
            try:
                element = instance(
                    items, seed=seed, path=f"{path}[{i}]",
                    depth=depth + 1, budget=budget,
                )
            except Unsatisfiable:
                # The same choice the optional properties make: an
                # element that cannot be built only sinks the array when
                # the array is required to have one.
                if _is_length(wanted) and wanted > 0:
                    raise
                return []
            # With `minItems` absent, zero elements are permitted, and
            # forcing one made `{"type":"array","items":{"type":"string",
            # "minLength":2,"maxLength":1}}` answer `["["]` — rejected by
            # its own schema, though `[]` satisfies it (Codex P2). One
            # item is the friendlier fixture and stays the default; it
            # is not worth returning an invalid document for.
            # The array's half of the same pair — flagged in passing by
            # the same review, and the third time this batch that the
            # object branch and the array branch needed the identical
            # change (decisions 99(e), 101(a)).
            if (
                not (_is_length(wanted) and wanted > 0)
                and isinstance(items, dict)
                and not _holds(element, items, budget)
            ):
                return []
            built.append(element)
        return built
    if declared == "boolean":
        return True
    if declared in ("integer", "number"):
        # Inside the declared bounds when there are any, so a strict
        # consumer of the reply is not handed something its own schema
        # would reject.
        return _numeric(schema, integral=declared == "integer", path=path)
    if declared == "null":
        return None

    text = f"{STUB_MARKER} {_word(seed, path)}"

    # `minLength` is honoured for the same reason the numeric branch
    # above stays inside `minimum`/`maximum`: the point of synthesising
    # an instance is that a strict consumer of the reply accepts it, and
    # a schema saying `{"type":"string","minLength":100}` got the fixed
    # 52-character marker and was rejected — keyless mode failing at
    # exactly the job it exists to do (Codex P2).
    minimum = schema.get("minLength")
    if _is_length(minimum) and minimum > len(text):
        if minimum > MAX_STRING:
            raise SchemaTooLarge(path)
        # Deterministic, like everything else here: the same schema and
        # seed give the same document, so a keyless run is reproducible.
        pad = _word(seed, f"{path}#pad")
        while len(text) < minimum:
            text += " " + pad
        text = text[:minimum]

    maximum = schema.get("maxLength")
    if _is_length(maximum) and maximum < len(text):
        # An upper bound wins over a lower one, as it does for numbers:
        # `minLength` above `maxLength` is a schema nothing can satisfy
        # FOR A STRING, and the bound that cannot be exceeded is the one
        # to keep. That clamp is right when the schema says `"type":
        # "string"` — nothing satisfies it, and this module answers a
        # caller's own contradiction with the coherent thing rather than
        # a refusal. It is wrong when no type is declared at all, which
        # is the case below.
        text = text[:maximum]

    if schema.get("type") is None and not _holds(text, schema, budget):
        # A string is this module's default, NOT the schema's
        # requirement. `minLength` and `maxLength` constrain strings and
        # nothing else, so `{"minLength":2,"maxLength":1}` is perfectly
        # satisfiable — by any non-string — and returning the clamped
        # one-character `"["` answered a satisfiable schema with a value
        # it rejects (Codex P2).
        #
        # This only runs when the default has ALREADY failed, so a
        # schema the string can satisfy — `{"minLength":100}`, padded
        # above — is untouched. The order prefers a real value and
        # leaves `null` last, the same preference the type-union branch
        # makes for the same reason.
        alternative = _other_type(schema, budget, avoid=("",))
        if alternative is not _NO_ALTERNATIVE:
            return alternative
        # Nothing else fits either, so the caller's schema is
        # contradictory whichever way it is read. Fall through to the
        # coherent string, as the declared-type case does.
    return text
