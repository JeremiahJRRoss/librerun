#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Does the synthesiser's test suite actually check what it claims?
#
# `services/gateway/tests/test_synth_validates.py` carries a HONOURED set
# — the JSON Schema keywords the stub's synthesiser promises to respect —
# and a gate requiring each to APPEAR in some corpus case. Appearing is
# not being checked. Five rounds of review found keywords that were
# claimed, present in a case, and still wrong.
#
# So this asks the harder question, keyword by keyword: inject a
# plausible defect into that keyword's handling and see whether anything
# fails. The HONOURED list says what is claimed; this says what is
# checked, and only the second is evidence.
#
#   bash scripts/synth_keyword_audit.sh
#
# Every case must report "caught". A "NOT CAUGHT" line names a keyword
# the suite would let regress silently — add the case that catches it.
# Restores services/gateway/gateway/synth.py from a snapshot taken
# before the run, and diffs the tree at the end: `git checkout` is NOT a
# restore here, it would revert uncommitted work to the last commit.
set -uo pipefail
cd "$(dirname "$0")/.."

SNAP="$(mktemp -d)"
cp services/gateway/gateway/synth.py "$SNAP/synth.py"
restore() { cp "$SNAP/synth.py" services/gateway/gateway/synth.py; }
trap restore EXIT

PY="${PY:-.venv/bin/python}"
SUITE="tests/test_synth_bounds.py tests/test_synth_fuzz.py tests/test_synth_validates.py"
failures=0

# Every case is written `inject ... && check "name"`, so a failed inject
# SKIPS its check — and a stale anchor used to leave `failures` untouched.
# The script printed ANCHOR MISSING twice and still ended with "every
# honoured keyword is checked", exit 0 (Codex P2): a gate reporting success
# by not looking, which is the one thing this repository's rules forbid
# outright. A missing anchor is counted here, at the point of failure, so
# the `&&` can never again swallow one. Bash functions do not subshell
# outside a pipeline, so this increment reaches the caller.
inject() {
  if _inject_py "$@"; then
    return 0
  fi
  failures=$((failures + 1))
  return 3
}

_inject_py() {
  python3 - "$@" <<'PYEOF'
import pathlib, sys
path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
source = pathlib.Path(path)
text = source.read_text()
occurrences = text.count(old)
if occurrences != 1:
    # Presence is not uniqueness. An anchor matching TWICE was replaced
    # at the first site and the case still reported "caught" — proving
    # something, but not necessarily the thing it names. Two of these
    # were live in this file, one of them created by the very fix that
    # added a second `probe = [budget[0]]`. A case that cannot say WHICH
    # site it injected is not evidence about either.
    what = "MISSING" if occurrences == 0 else f"AMBIGUOUS ({occurrences} sites)"
    print(f"  *** ANCHOR {what} in {path} — this case proves nothing ***")
    sys.exit(3)
source.write_text(text.replace(old, new, 1))
PYEOF
}

run_suite() {
  (cd services/gateway && timeout 150 "../../$PY" -m pytest -q $SUITE 2>&1)
}

# A nonzero pytest exit is not evidence that the mutation was caught.
# Collection errors, an import failure, a missing dependency, an
# interpreter that does not exist — all of them exit nonzero without
# running a single test, and this script used to print `caught` for
# every one of them. `PY=/bin/false bash scripts/synth_keyword_audit.sh`
# reported all twenty cases caught, emitted the success line and exited
# 0 with nothing executed (Codex P2). A caught mutation is one where
# pytest actually RAN and actually FAILED, which it says by counting:
# `N failed`. No count means the suite never reached a verdict, and that
# is an audit failure, not a catch.
#
# This is the previous round's finding one level deeper. That one was
# "a skipped check is silent"; this one is "a check that never ran looks
# exactly like a caught mutation". Both are the same sin — reporting
# success without looking — and the tell was in the output the whole
# time, as an empty pair of brackets: `minLength: caught ()`.
check() {
  local name="$1"
  local out rc count
  out=$(run_suite)
  rc=$?
  count=$(printf '%s' "$out" | grep -oE '[0-9]+ failed' | head -1)
  if [ $rc -eq 124 ]; then
    echo "  $name: TIMED OUT — a hang is not a pass"; failures=$((failures + 1))
  elif [ $rc -ne 0 ] && [ -n "$count" ]; then
    echo "  $name: caught ($count)"
  elif [ $rc -ne 0 ]; then
    echo "  $name: *** NO VERDICT — pytest exited $rc without running tests ***"
    printf '%s\n' "$out" | tail -3 | sed 's/^/      /'
    failures=$((failures + 1))
  else
    echo "  $name: *** NOT CAUGHT ***"; failures=$((failures + 1))
  fi
  restore
}

# The premise of every case below is that the suite passes UNMUTATED.
# Against an already-red suite each injection would be "caught" by a
# failure it had nothing to do with, so the baseline is established
# first and the run refuses to start without it.
baseline() {
  local out rc
  echo "Baseline (the suite must be green before any mutation means anything):"
  out=$(run_suite)
  rc=$?
  if [ $rc -eq 0 ]; then
    echo "  green ($(printf '%s' "$out" | grep -oE '[0-9]+ passed' | head -1))"
    return 0
  fi
  echo "  *** NOT GREEN — pytest exited $rc. Every case below would be"
  echo "      meaningless, so the audit refuses to run. ***"
  printf '%s\n' "$out" | tail -5 | sed 's/^/      /'
  exit 1
}

baseline
echo
echo "Injecting one defect per honoured keyword:"

inject services/gateway/gateway/synth.py \
  '    minimum = schema.get("minLength")
    if _is_length(minimum) and minimum > len(text):' \
  '    minimum = schema.get("minLength")
    if False:' && check "minLength"

inject services/gateway/gateway/synth.py \
  '    maximum = schema.get("maxLength")
    if _is_length(maximum) and maximum < len(text):' \
  '    maximum = schema.get("maxLength")
    if False:' && check "maxLength"

inject services/gateway/gateway/synth.py \
  '        count = max(wanted, 1) if _is_length(wanted) else 1' \
  '        count = 1' && check "minItems"

inject services/gateway/gateway/synth.py \
  '        maximum = schema.get("maxItems")
        if _is_length(maximum):
            count = min(count, max(maximum, 0))' \
  '        pass' && check "maxItems"

inject services/gateway/gateway/synth.py \
  '    if "const" in schema:
        return [schema["const"]]' \
  '    if False:
        return [schema["const"]]' && check "const"

inject services/gateway/gateway/synth.py \
  '        for key in wanted:' '        for key in wanted[:1]:' && check "properties"

inject services/gateway/gateway/synth.py \
  '        wanted = required or list(properties)' \
  '        wanted = [k for k in required if k in properties] or list(properties)' \
  && check "required"

inject services/gateway/gateway/synth.py \
  '    low, low_open = _lower(schema)
    high, high_open = _upper(schema)

    if integral:' \
  '    low, low_open = (None, False)
    high, high_open = _upper(schema)

    if integral:' && check "minimum / exclusiveMinimum"

inject services/gateway/gateway/synth.py \
  '    low, low_open = _lower(schema)
    high, high_open = _upper(schema)

    if integral:' \
  '    low, low_open = _lower(schema)
    high, high_open = (None, False)

    if integral:' && check "maximum / exclusiveMaximum"

inject services/gateway/gateway/synth.py \
  '                try:
                    out[key] = _merged([out[key], value], {}, budget)' \
  '                try:
                    pass' && check "items"

inject services/gateway/gateway/synth.py \
  '                out[key] = _narrowed_type(out.get(key), value)' \
  '                out.setdefault(key, value)' && check "type"

inject services/gateway/gateway/synth.py \
  '        allowed = _intersect(allowed, _allowed_values(branch), budget)' \
  '        pass' && check "enum / allOf"

inject services/gateway/gateway/synth.py \
  '                if _holds(candidate, siblings, budget) and any(
                    _holds(candidate, option, budget) for option in options
                ):
                    return candidate' \
  '                if True:
                    return candidate' && check "anyOf"

inject services/gateway/gateway/synth.py \
  '                if matched == 1 and _holds(candidate, siblings, budget):
                    return candidate' \
  '                if True:
                    return candidate' && check "oneOf"

inject services/gateway/gateway/synth.py \
  '    if "default" in schema and _holds(schema["default"], schema, budget):' \
  '    if "default" in schema:' && check "default"

# The keyword list is not the only thing worth an injection. These are
# the structural properties this module has now been reviewed into —
# each one a defect Codex found in a previous round's fix, and each one
# invisible to a keyword audit because no keyword names it.
echo
echo "Injecting one defect per structural property:"

inject services/gateway/gateway/synth.py \
  '            return _merged([option], _rest, budget)' \
  '            return _merged([option], _rest)' && check "one shared budget"

inject services/gateway/gateway/synth.py \
  '        def _branch(option, _rest=rest, _extra=with_sibling):' \
  '        _eager = {id(o): _merged([o] + with_sibling, rest, budget) for o in options}

        def _branch(option, _rest=rest, _e=_eager, _x=with_sibling):
            return _e[id(option)]' && check "branch merges stay lazy"

inject services/gateway/gateway/synth.py \
  '        elif inclusive:
            out["anyOf"] = _distributed(inclusive, budget)' \
  '        elif inclusive:
            out["anyOf"] = inclusive[0][1]' && check "every combinator carried"

inject services/gateway/gateway/synth.py \
  '                if _type_overlap(combo_kind, kind) == set():
                    continue' \
  '                if False:
                    continue' && check "impossible combinations dropped"

inject services/gateway/gateway/synth.py \
  '    permitted = schema.get("enum")
    if isinstance(permitted, list) and not _member(value, permitted' \
  '    permitted = schema.get("enum")
    if False and isinstance(permitted, list) and not _member(value, permitted' \
  && check "_satisfies evaluates enum"

inject services/gateway/gateway/synth.py \
  '    if "const" in schema and not _same(value, schema["const"], depth):' \
  '    if False and not _same(value, schema["const"], depth):' \
  && check "_satisfies evaluates const"

inject services/gateway/gateway/synth.py \
  '        options = [o for o in (schema.get(name) or []) if isinstance(o, dict)]
        if not options:
            continue
        rest = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf")}' \
  '        options = [o for o in (schema.get(name) or []) if isinstance(o, dict)]
        if True:
            continue
        rest = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf")}' \
  && check "oneOf searches nested alternatives"

inject services/gateway/gateway/synth.py \
  '        if len(members) > 1:' \
  '        if False:' && check "each union member tried"

inject services/gateway/gateway/synth.py \
  '            if key not in compulsory and isinstance(sub, dict) and not _holds(
                value, sub, budget
            ):' \
  '            if False:' && check "unsatisfiable optional properties omitted"

inject services/gateway/gateway/synth.py \
  '    permitted = _allowed_values(schema)
    if permitted is not None:
        for value in permitted:' \
  '    permitted = None
    if permitted is not None:
        for value in permitted:' && check "enum leaves offer every value"

inject services/gateway/gateway/synth.py \
  '            try:
                merged.append(_branch(option))
            except Unsatisfiable:
                merged.append(None)' \
  '            merged.append(_branch(option))' \
  && check "an impossible branch is skipped"

inject services/gateway/gateway/synth.py \
  '    if "null" in names:
        members.append("null")
    return members' \
  '    if False:
        members.append("null")
    return members' && check "null stays a union member"

inject services/gateway/gateway/synth.py \
  '                and isinstance(items, dict)
                and not _holds(element, items, budget)' \
  '                and isinstance(items, dict)
                and False' && check "array prefers empty over invalid"

inject services/gateway/gateway/synth.py \
  '                if _holds(candidate, siblings, budget) and any(
                    _holds(candidate, option, budget) for option in options
                ):' \
  '                if any(
                    _satisfies(candidate, option) for option in options
                ):' && check "anyOf membership evaluates combinators"

inject services/gateway/gateway/synth.py \
  '                matched = sum(
                    1 for other in options if _holds(candidate, other, budget)
                )
                if matched == 1 and _holds(candidate, siblings, budget):' \
  '                matched = sum(
                    1 for other in merged if other is not None
                    and _satisfies(candidate, other)
                )
                if matched == 1:' && check "oneOf counts the original options"

inject services/gateway/gateway/synth.py \
  '    properties = schema.get("properties")
    if isinstance(value, dict) and isinstance(properties, dict):' \
  '    properties = None
    if isinstance(value, dict) and isinstance(properties, dict):' \
  && check "_holds walks object subschemas"

inject services/gateway/gateway/synth.py \
  '    items = schema.get("items")
    if isinstance(value, list) and isinstance(items, dict):' \
  '    items = None
    if isinstance(value, list) and isinstance(items, dict):' \
  && check "_holds walks array items"

inject services/gateway/gateway/synth.py \
  '        siblings = {k: v for k, v in schema.items() if k != combinator}' \
  '        siblings = rest' && check "sibling combinators are enforced"

inject services/gateway/gateway/synth.py \
  '        with_sibling = [{other: sibling}] if sibling else []' \
  '        with_sibling = []' && check "a sibling combinator generates"

inject services/gateway/gateway/synth.py \
  '                    merged = _merged([option] + _extra, _rest, probe)' \
  '                    merged = _merged([option] + _extra, _rest)' \
  && check "one shared budget on the sibling path"

inject services/gateway/gateway/synth.py \
  '        for branch, from_sibling in itertools.chain(
            ((b, False) for b in merged),
            ((b, True) for b in _sibling_seeds()),
        ):' \
  '        for branch, from_sibling in [(b, False) for b in merged] + [
            (b, True) for b in list(_sibling_seeds())
        ]:' \
  && check "sibling seeds are drawn lazily"

inject services/gateway/gateway/synth.py \
  '        for branch, from_sibling in itertools.chain(
            ((b, False) for b in merged),
            ((b, True) for b in _sibling_seeds()),
        ):' \
  '        for branch, from_sibling in ((b, False) for b in merged):' \
  && check "the sibling seeds the search"

inject services/gateway/gateway/synth.py \
  '        # mistake decision 104 just fixed. The total stays bounded —
        # two fixed caps — and the real bound on work is the budget.
        seed_limit = [MAX_CANDIDATES]' \
  '        # mistake decision 104 just fixed. The total stays bounded —
        # two fixed caps — and the real bound on work is the budget.
        seed_limit = limit' && check "sibling seeds have their own allowance"

# The same three questions asked of `_candidates`, the RECURSIVE expander,
# which had no sibling mechanism at all: `rest` drops both combinators, so a
# nested sibling could veto candidates through the consumer's `_holds` and
# never supply one (decision 115).
inject services/gateway/gateway/synth.py \
  '        for option in (schema.get(other) or []):' \
  '        for option in []:' \
  && check "a NESTED sibling supplies candidates"

inject services/gateway/gateway/synth.py \
  '        other = "anyOf" if name == "oneOf" else "oneOf"
        seed_limit = [MAX_CANDIDATES]' \
  '        other = "anyOf" if name == "oneOf" else "oneOf"
        seed_limit = limit' \
  && check "nested sibling seeds have their own allowance"

inject services/gateway/gateway/synth.py \
  '            probe = _probe(budget)
            allowed = probe[0]
            try:
                seeded = _merged([option], rest, probe)' \
  '            probe = budget
            allowed = probe[0]
            try:
                seeded = _merged([option], rest, probe)' \
  && check "a failed NESTED seed merge does not bankrupt the search"

inject services/gateway/gateway/synth.py \
  '                if _holds(value, schema, budget):
                    yield value' \
  '                if True:
                    yield value' \
  && check "a nested seed no arm accepts is not the answer"

inject services/gateway/gateway/synth.py \
  '            if key not in compulsory and isinstance(sub, dict) and not _holds(
                value, sub, budget
            ):' \
  '            if key not in compulsory and isinstance(sub, dict) and not _satisfies(
                value, sub
            ):' && check "optional properties judged through combinators"

inject services/gateway/gateway/synth.py \
  '                and not _holds(element, items, budget)' \
  '                and not _satisfies(element, items)' \
  && check "optional array items judged through combinators"

inject services/gateway/gateway/synth.py \
  '        if len(exclusive) > 1:' \
  '        if False:' && check "two oneOf groups still refuse"

inject services/gateway/gateway/synth.py \
  '    hashable, unhashable = _enum_index(permitted, budget)
    if key in hashable:
        return True' \
  '    hashable, unhashable = (set(), list(permitted))
    if key in hashable:
        return True' \
  && check "enum membership goes through the index"

inject services/gateway/gateway/synth.py \
  '    if len(budget) < 2:
        budget.append({"allowance": [MAX_VALUE_WORK]})' \
  '    if len(budget) < 2:
        budget.append({"allowance": budget})' \
  && check "value work is charged apart from the node budget"

inject services/gateway/gateway/synth.py \
  '                probe = _probe(budget)
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
                yield seeded' \
  '                try:
                    yield _merged([option], _rest, budget)
                except (Unsatisfiable, SchemaTooLarge):
                    continue' \
  && check "a failed seed merge does not bankrupt the search"

inject services/gateway/gateway/synth.py \
  '    enum = schema.get("enum")
    if isinstance(enum, list):
        return enum' \
  '    enum = schema.get("enum")
    if isinstance(enum, list):
        return list(enum)' \
  && check "an equal enum is not indexed again"

inject services/gateway/gateway/synth.py \
  '        # Also not copied, and for the same reason: an intersection
        # against nothing IS the incoming list, and returning a fresh
        # object here breaks the index cache exactly as the defensive
        # copy in `_allowed_values` did.
        return incoming' \
  '        return list(incoming)' \
  && check "an intersection against nothing keeps identity"

inject services/gateway/gateway/synth.py \
  '    if schema.get("type") is None and not _holds(text, schema, budget):' \
  '    if False and not _holds(text, schema, budget):' \
  && check "untyped conflicting lengths try a non-string"

inject services/gateway/gateway/synth.py \
  '            if _holds(candidate, schema, budget):
                return candidate
        raise Unsatisfiable(path)' \
  '            if _satisfies(candidate, schema, 0, budget):
                return candidate
        raise Unsatisfiable(path)' \
  && check "enum selection sees combinators in containers"

inject services/gateway/gateway/synth.py \
  '    if declared is None and "properties" in schema:' \
  '    if False and "properties" in schema:' \
  && check "untyped properties do not force an object"

inject services/gateway/gateway/synth.py \
  '    for alternative in _OTHER_TYPES:
        if any(alternative is skip for skip in avoid):
            continue
        if _holds(alternative, schema, budget):
            return alternative
    return _NO_ALTERNATIVE' \
  '    return _NO_ALTERNATIVE' \
  && check "another JSON type is searched when the inferred one fails"

inject services/gateway/gateway/synth.py \
  '            if _holds(value, schema, budget) and _claim(value, seen, budget):
                limit[0] -= 1
                yield value' \
  '            limit[0] -= 1
            if _satisfies(value, schema, 0, budget) and _claim(value, seen, budget):
                yield value' \
  && check "a rejected enum leaf spends no candidate capacity"

inject services/gateway/gateway/synth.py \
  '    try:
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
        yield value' \
  '    limit[0] -= 1
    try:
        value = instance(schema, seed=seed, path=path, depth=depth, budget=budget)
    except Unsatisfiable:
        return
    if _claim(value, seen, budget):
        yield value' \
  && check "a leaf that cannot be built spends no capacity"

inject services/gateway/gateway/synth.py \
  '                allowance[0] += 1' \
  '                pass' \
  && check "a contextually rejected candidate gives its capacity back"

# The same question in the NESTED seed loop, which round 49's filter left
# unanswered: a seed the filter rejects had already been deducted, and the
# boundary sat on `MAX_CANDIDATES` itself (decision 116).
inject services/gateway/gateway/synth.py \
  '                _release(value, seen, budget)
                if seed_refunds[0] > 0:
                    seed_refunds[0] -= 1
                    seed_limit[0] += 1' \
  '                _release(value, seen, budget)
                if seed_refunds[0] > 0:
                    seed_refunds[0] -= 1' \
  && check "a rejected NESTED seed gives its capacity back"

inject services/gateway/gateway/synth.py \
  '        # exists to protect.
        seed_refunds = [MAX_CANDIDATES]' \
  '        # exists to protect.
        seed_refunds = [10 ** 9]' \
  && check "nested seed refunds are themselves bounded"

# `1.0` is an integer in JSON Schema, so exclusivity can turn on the
# integer/number boundary and nothing `instance` returns ever sits on the
# non-integral side of it (decision 117).
inject services/gateway/gateway/synth.py \
  '            for offset in (step - 0.5, -(step - 0.5), step, -step):' \
  '            for offset in ():' \
  && check "a numeric leaf offers a non-integral candidate"

# And that candidate must not narrow the search it widens: charged to
# `limit`, a numeric leaf spends two slots and a crowded `oneOf` reaches
# half as far (decisions 90/116).
inject services/gateway/gateway/synth.py \
  '    neighbour_limit = state.setdefault("neighbour", [MAX_CANDIDATES])' \
  '    neighbour_limit = limit' \
  && check "the non-integral candidate has an allowance of its own"

# That allowance is spent on YIELD, before the outer combinator judges the
# value, so a locally valid fraction rejected by context must give its slot
# back — the third counter to need what the first two already had
# (decision 118).
inject services/gateway/gateway/synth.py \
  '                    if neighbour_refunds[0] > 0:
                        neighbour_refunds[0] -= 1
                        neighbour_limit[0] += 1' \
  '                    if neighbour_refunds[0] > 0:
                        neighbour_refunds[0] -= 1' \
  && check "a context-rejected fraction gives its capacity back"

# And the refunded capacity has to be SPENT: `+0.5` and `-0.5` are two
# candidates, not one with a spare, and an unconditional `return` here
# threw the refund away on the line that granted it (decision 119).
inject services/gateway/gateway/synth.py \
  '            for offset in (step - 0.5, -(step - 0.5), step, -step):' \
  '            for offset in (step - 0.5,):' \
  && check "both fractional offsets are offered"

# A leaf offers INTEGRAL neighbours too. `+-0.5` cannot satisfy an
# `integer` arm, so when the synthesised integer lands in an overlapping
# branch the fractional offsets leave the leaf with nothing (decision 120).
inject services/gateway/gateway/synth.py \
  '            for offset in (step - 0.5, -(step - 0.5), step, -step):' \
  '            for offset in (step - 0.5, -(step - 0.5)):' \
  && check "a numeric leaf offers an integral neighbour"

# And the walk goes PAST an occupied neighbour. Both immediate integers can
# themselves match the competing branch, and so can both fractions — the
# sibling half of the same question (decision 121).
inject services/gateway/gateway/synth.py \
  '        for step in range(1, _MAX_NEIGHBOUR_STEPS + 1):' \
  '        for step in range(1, 2):' \
  && check "the neighbour walk goes past an occupied step"

# One leaf must not walk its whole ring and leave the next leaf nothing:
# the distance bound and the per-leaf yield bound answer different
# questions (decision 121).
inject services/gateway/gateway/synth.py \
  '                if neighbour_limit[0] <= 0 or offered >= _MAX_NEIGHBOUR_YIELDS:' \
  '                if neighbour_limit[0] <= 0:' \
  && check "one leaf cannot spend the whole neighbour allowance"

# And a leaf whose whole ring is refused stops walking, or the refused
# `_holds` calls spend the node budget a schema needed to ANSWER.
inject services/gateway/gateway/synth.py \
  '            if not admissible:
                # Every offset in this ring was refused by the leaf itself,' \
  '            if False:
                # Every offset in this ring was refused by the leaf itself,' \
  && check "a leaf whose ring is refused stops walking"

# At large magnitudes the fixed offsets vanish into the float's own
# precision, so the walk has to step to a REPRESENTABLE neighbour —
# `_step_inside` already knew that at a bound (decision 122).
inject services/gateway/gateway/synth.py \
  '                if neighbour == value:' \
  '                if False:' \
  && check "the walk steps to a representable neighbour"

# And an int leaf keeps its exact steps rather than falling into the float
# path, which answers a rounded float where an exact integer exists.
inject services/gateway/gateway/synth.py \
  '                if isinstance(value, int) and not isinstance(offset, int):' \
  '                if False:' \
  && check "a large integer leaf keeps exact steps"

inject services/gateway/gateway/synth.py \
  '    neighbour_refunds = state.setdefault("neighbour_refunds", [MAX_CANDIDATES])' \
  '    neighbour_refunds = state.setdefault("neighbour_refunds", [10 ** 9])' \
  && check "fractional refunds are themselves bounded"

inject services/gateway/gateway/synth.py \
  '        if exclusive:' \
  '        if False:' \
  && check "a single oneOf beside anyOf is carried as a sibling"

inject services/gateway/gateway/synth.py \
  '                    # exceed its budget by attrition.
                    continue
                except SchemaTooLarge:' \
  '                    # exceed its budget by attrition.
                    raise
                except SchemaTooLarge:' \
  && check "an unusable product is skipped, not fatal"

inject services/gateway/gateway/synth.py \
  '                            merged_props[name] = {"enum": []}' \
  '                            raise' \
  && check "a contradictory property does not sink the object"

inject services/gateway/gateway/synth.py \
  '                    out[key] = {"enum": []}' \
  '                    raise' \
  && check "a contradictory item does not sink an empty-capable array"
# Round 58. A candidate the search has already offered is not a new
# candidate, and must not spend the capacity meant for one — the third
# costume of the rule that rejected and unbuildable leaves already obey.

inject services/gateway/gateway/synth.py \
  '                    if not _claim(neighbour, seen, budget):' \
  '                    if False and not _claim(neighbour, seen, budget):' \
  && check "the neighbour walk counts candidates, not attempts"

inject services/gateway/gateway/synth.py \
  '            if _holds(value, schema, budget) and _claim(value, seen, budget):' \
  '            if _holds(value, schema, budget):' \
  && check "a repeated enum member spends no candidate capacity"

inject services/gateway/gateway/synth.py \
  '    if _claim(value, seen, budget):
        limit[0] -= 1
        yield value' \
  '    if True:
        limit[0] -= 1
        yield value' \
  && check "repeated arms do not starve the arm behind them"

inject services/gateway/gateway/synth.py \
  '                _release(value, seen, budget)' \
  '                pass' \
  && check "a dropped seed gives its claim back"

inject services/gateway/gateway/synth.py \
  '        keys.discard(key)
        return' \
  '        return' \
  && check "the release actually releases"


# Round 59. A list or an object is a value like any other: the index has
# to key it, or every place that asks "have I got this already" answers
# no for exactly the values it cannot hash.

inject services/gateway/gateway/synth.py \
  '    if isinstance(value, list):
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
        built = ("array", tuple(items))' \
  '    if isinstance(value, list):
        return None' \
  && check "an array candidate is keyed, not called fresh"

inject services/gateway/gateway/synth.py \
  '        members = []
        for name in sorted(value):
            if allowance is not None:
                _spend(allowance, "$")
            key = _key(value[name], depth + 1, budget)
            if key is None:
                return None
            members.append((name, key))
        built = ("object", tuple(members))' \
  '        return None
        members = []' \
  && check "an object candidate is keyed, not called fresh"

inject services/gateway/gateway/synth.py \
  '        for name in sorted(value):' \
  '        for name in value:' \
  && check "an object key does not depend on member order"

inject services/gateway/gateway/synth.py \
  '    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float)):
        return ("number", value)
    if isinstance(value, str):' \
  '    if isinstance(value, (int, float)):
        return ("number", value)
    if isinstance(value, str):' \
  && check "the key keeps bool apart from 1 at every depth"

inject services/gateway/gateway/synth.py \
  '    if depth > _MAX_DEPTH:
        return None
    if isinstance(value, bool):' \
  '    if depth > 1:
        return None
    if isinstance(value, bool):' \
  && check "the key reaches as deep as _same does"

# Round 60. Keying a container is a walk proportional to its size, so it
# has to be memoised (or the callers repeat it) AND charged (or a request
# can walk unboundedly). And a value JSON cannot write is not an answer.

inject services/gateway/gateway/synth.py \
  '        held = memo.get(id(value))
        # `is`, not `==`: an id can be reused once the object it named is
        # gone, and the memo holding the value is what stops that here.
        if held is not None and held[0] is value:
            return held[1]
        allowance = state["allowance"]' \
  '        allowance = state["allowance"]' \
  && check "a container key is memoised for the request"

inject services/gateway/gateway/synth.py \
  '            if allowance is not None:
                _spend(allowance, "$")
            key = _key(value[name], depth + 1, budget)' \
  '            key = _key(value[name], depth + 1, budget)' \
  && check "keying a wide value is charged per member"

inject services/gateway/gateway/synth.py \
  '    if not _representable(value, budget):' \
  '    if False and not _representable(value, budget):' \
  && check "a value JSON cannot write is not an answer"

inject services/gateway/gateway/synth.py \
  '    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, int):
        # Unbounded in Python and a perfectly good JSON number. Asking
        # `math.isfinite` would convert it and raise (§12 123).
        return True' \
  '    if isinstance(value, (int, float)):
        return math.isfinite(value)' \
  && check "a huge integer is still a JSON number"

inject services/gateway/gateway/synth.py \
  '    members = value if isinstance(value, list) else value.values()
    ok = True
    for member in members:' \
  '    members = []
    ok = True
    for member in members:' \
  && check "non-finite is refused inside a container too"

# Round 61. A speculative probe gets its own node count and the SAME
# value state — allowance and memo — or the charged walks under it are
# charged to nobody and the bound has a door in it.

inject services/gateway/gateway/synth.py \
  '    _value_state(budget)
    return [budget[0], budget[1]]' \
  '    _value_state(budget)
    return [budget[0]]' \
  && check "a speculative probe shares the value state"

inject services/gateway/gateway/synth.py \
  '            probe = _probe(budget)
            allowed = probe[0]
            try:
                seeded = _merged([option], rest, probe)' \
  '            probe = [budget[0]]
            allowed = probe[0]
            try:
                seeded = _merged([option], rest, probe)' \
  && check "no speculative budget is built by hand"

# Round 62. Once a probe shares the value allowance, `SchemaTooLarge` can
# mean the REQUEST is over rather than this option being too big. The two
# must not be caught together, and the shared exhaustion must re-raise.

inject services/gateway/gateway/synth.py \
  '            except SchemaTooLarge:
                # Too big to BUILD is survivable; out of value work is
                # not, and since round 61 both arrive here as the same
                # exception.
                if _value_exhausted(budget):
                    raise
                continue' \
  '            except SchemaTooLarge:
                continue' \
  && check "a nested seed does not swallow value exhaustion"

inject services/gateway/gateway/synth.py \
  '    return _value_state(budget)["allowance"][0] < 0' \
  '    return False' \
  && check "value exhaustion is actually detected"

# Round 63. A contradiction nested inside `allOf` is still a
# contradiction, and a boolean leaf has two values.

inject services/gateway/gateway/synth.py \
  '        inner = _effective_type(branch, depth + 1)
        if inner is None:
            continue
        if inner == set():
            return set()' \
  '        inner = _effective_type(branch, depth + 1)
        if inner is None:
            continue' \
  && check "a contradiction nested in allOf is reported"

inject services/gateway/gateway/synth.py \
  '    declared = schema.get("type")
    for branch in (schema.get("allOf") or []):' \
  '    declared = schema.get("type")
    for branch in ():' \
  && check "the effective type looks through allOf"

inject services/gateway/gateway/synth.py \
  '                if _type_overlap(combo_kind, kind) == set():
                    continue' \
  '                if False:
                    continue' \
  && check "an impossible product is dropped before it is merged"

inject services/gateway/gateway/synth.py \
  '        other = not value
        if neighbour_limit[0] > 0 and _holds(other, schema, budget):' \
  '        other = not value
        if False and _holds(other, schema, budget):' \
  && check "a boolean leaf offers its other value"

inject services/gateway/gateway/synth.py \
  '            if _claim(other, seen, budget):
                neighbour_limit[0] -= 1
                yield other' \
  '            if True:
                neighbour_limit[0] -= 1
                yield other' \
  && check "the boolean alternate is claimed like any candidate"

inject services/gateway/gateway/synth.py \
  '    declared = schema.get("type")
    if isinstance(declared, list):
        members = _union_members(declared)' \
  '    declared = None
    if isinstance(declared, list):
        members = _union_members(declared)' \
  && check "a union leaf offers every declared type"

inject services/gateway/gateway/synth.py \
  '        for name in members:
            if neighbour_limit[0] <= 0:
                break
            # On a PROBE budget, the fifth site to take this pattern and' \
  '        for name in members:
            # On a PROBE budget, the fifth site to take this pattern and' \
  && check "the union alternate is bounded by the shared allowance"

inject services/gateway/gateway/synth.py \
  '            if not _holds(other, schema, budget) or not _claim(
                other, seen, budget
            ):
                continue' \
  '            if not _holds(other, schema, budget):
                continue' \
  && check "the union alternate is claimed like any candidate"

# The null-last block appears TWICE — `instance` has the identical three
# lines for its own union loop — so the anchor carries the line after it,
# which only `_candidates` has. An ambiguous anchor injects into whichever
# site `replace` reaches first, and a case that cannot name its own site
# proves nothing.
inject services/gateway/gateway/synth.py \
  '    members = [name for name in names if name != "null"]
    if "null" in names:
        members.append("null")
    return members' \
  '    members = list(names)
    return members' \
  && check "the union offers null last"

inject services/gateway/gateway/synth.py \
  '                other = instance(
                    {**schema, "type": name}, seed=seed, path=path,
                    depth=depth + 1, budget=probe,
                )' \
  '                other = instance(
                    {**schema, "type": name}, seed=seed, path=path,
                    depth=depth + 1, budget=budget,
                )' \
  && check "an oversized union member is built on a probe"

inject services/gateway/gateway/synth.py \
  '            except SchemaTooLarge:
                if _value_exhausted(budget):
                    raise
                if discarded[0] > 0:' \
  '            except SchemaTooLarge:
                if discarded[0] > 0:' \
  && check "the union member keeps the two refusals apart"

inject services/gateway/gateway/synth.py \
  '            _settle(budget, probe, allowed)
            # Claimed before it is charged, like every other candidate' \
  '            # Claimed before it is charged, like every other candidate' \
  && check "a union member that built pays for what it built"

# The union refund does NOT draw on the shared cap, so the injection is to
# make it draw on one — the state the round-64 head shipped in.
inject services/gateway/gateway/synth.py \
  '            neighbour_limit[0] -= 1
            yield other
            # Refunded WITHOUT drawing on the shared refund cap' \
  '            neighbour_limit[0] -= 1
            yield other
            if neighbour_refunds[0] > 0:
                neighbour_refunds[0] -= 1
            else:
                neighbour_limit[0] -= 1
            # Refunded WITHOUT drawing on the shared refund cap' \
  && check "union alternates do not starve a later leaf"

inject services/gateway/gateway/synth.py \
  '        if isinstance(name, str) and name not in seen:' \
  '        if isinstance(name, str) and False:' \
  && check "duplicate union members are offered once"

inject services/gateway/gateway/synth.py \
  '            probe = _probe(budget)
            if discarded[0] <= 0:
                probe[0] = min(probe[0], _MAX_CHEAP_MEMBER_NODES)' \
  '            probe = _probe(budget)' \
  && check "discarded member probes are bounded"

inject services/gateway/gateway/synth.py \
  '                if discarded[0] > 0:
                    discarded[0] -= 1
                continue' \
  '                continue' \
  && check "a discarded member probe is charged for"

inject services/gateway/gateway/synth.py \
  '    seen = set()
    names = []' \
  '    seen = []
    names = []' \
  && check "union deduplication is linear"

inject services/gateway/gateway/synth.py \
  '_MAX_CHEAP_MEMBER_NODES = 64' \
  '_MAX_CHEAP_MEMBER_NODES = 10 ** 9' \
  && check "the narrowed probe is actually narrowed"

inject services/gateway/gateway/synth.py \
  '                probe[0] = min(probe[0], _MAX_CHEAP_MEMBER_NODES)' \
  '                probe[0] = min(probe[0], 2)' \
  && check "a narrowed probe still fits a cheap member"

inject services/gateway/gateway/synth.py \
  '    budget[0] -= allowed - probe[0]' \
  '    budget[0] = probe[0]' \
  && check "a probe settles its spend, not its remainder"

inject services/gateway/gateway/synth.py \
  '            probe = _probe(budget)
            if discarded[0] <= 0:
                probe[0] = min(probe[0], _MAX_CHEAP_MEMBER_NODES)
            # What the probe was ALLOWED, read after any narrowing,' \
  '            probe = _probe(budget)
            allowed = probe[0]
            if discarded[0] <= 0:
                probe[0] = min(probe[0], _MAX_CHEAP_MEMBER_NODES)
            # What the probe was ALLOWED, read after any narrowing,' \
  && check "the narrowed site reads its allowance after narrowing"

inject services/gateway/gateway/synth.py \
  '                if merged is not None:
                    _settle(budget, probe, allowed)' \
  '                if merged is not None:
                    pass' \
  && check "every probe site settles"

restore
# Compare against the SNAPSHOT, not against git. `git diff` answers
# "does this differ from the last commit", which is true of any tree
# with uncommitted work — that is, exactly the tree you run this on
# while developing, so the check fired on every such run and said
# "restore failed" when the restore was fine. The reference for "did my
# restore work" is the copy taken before the run, and nothing else.
# Same mistake as reaching for `git checkout` as a restore.
if ! cmp -s "$SNAP/synth.py" services/gateway/gateway/synth.py; then
  echo "the audit left synth.py changed — restore failed"; exit 1
fi
if [ "$failures" -ne 0 ]; then
  echo; echo "$failures case(s) the suite would let regress silently."; exit 1
fi
echo; echo "every honoured keyword and structural property is checked by something that bites."
