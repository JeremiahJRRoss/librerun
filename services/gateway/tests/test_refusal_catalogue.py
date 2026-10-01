"""§10 of the authoring page is a promise, so it is derived, not typed.

`docs/authoring/LLM_Gateway.md` §10 lists every refusal an agent author
can be handed. A hand-maintained table drifts the moment a refusal is
added — and it had: this batch shipped `invocation_deadline`,
`invalid_json`, `request_too_large` and `unrewritable_value` without
rows, while the table promised nothing that did not exist only by luck.

So the check is scoped to the code rather than to a list someone
remembered (the round-12 lesson, one document over). It walks the
gateway's own source for every place a refusal code is produced, and
requires the table and the code to agree in BOTH directions:

- a code the gateway can raise and the table does not carry is a
  refusal an author meets with no documentation at all;
- a code the table carries and the gateway cannot raise is a promise
  the gateway does not keep — the worse of the two, because a reader
  cannot tell it from a real one.

The one refusal whose code is not a literal — `require()` builds
`f"{capability}_not_granted"` — is derived from its call sites rather
than written down here. A refusal added with a computed code that this
walk cannot follow fails the test by name instead of slipping through:
an undocumented refusal is exactly what this exists to prevent.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

GATEWAY = pathlib.Path(__file__).resolve().parents[1] / "gateway"
DOC = (
    pathlib.Path(__file__).resolve().parents[3]
    / "docs"
    / "authoring"
    / "LLM_Gateway.md"
)

# Where the first argument is the code. ``unavailable`` joined them at
# blueprint S4c — same shape, same position, and leaving it out would
# have made its refusal invisible to this walk rather than noisy.
_CODE_FIRST = {"unauthorized", "forbidden", "bad_request", "unavailable", "_Refusal"}
# Where the second is (status comes first).
_CODE_SECOND = {"GatewayError"}


def _callee(node: ast.Call) -> str | None:
    fn = node.func
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return getattr(fn, "id", None)


# The one refusal whose code is built rather than written: `require()`
# raises `forbidden(f"{capability}_not_granted", ...)`. The suffix is
# pinned here so renaming it breaks this check rather than quietly
# emptying it, and the capabilities themselves come from the literal
# `require("...")` call sites — no list to keep in step.
_GRANT_SUFFIX = "_not_granted"


def _derived_grant_code(arg: ast.AST) -> bool:
    """`f"{x}_not_granted"` — a code this walk knows how to follow."""
    if not isinstance(arg, ast.JoinedStr) or len(arg.values) != 2:
        return False
    head, tail = arg.values
    return (
        isinstance(head, ast.FormattedValue)
        and isinstance(tail, ast.Constant)
        and tail.value == _GRANT_SUFFIX
    )


def _raised_codes() -> tuple[set[str], list[str]]:
    """Every code the gateway can produce, and every site it cannot read.

    Three kinds of site, and only the third is a problem:

    - a **literal** produces that code;
    - a bare name or attribute **forwards** one produced elsewhere —
      `errors.bad_request(refusal.code, ...)`, and the three factory
      functions themselves — so the producing site has already counted
      it;
    - anything that BUILDS a string is opaque, and fails the third test
      below, unless it is the one shape this walk follows.
    """
    codes: set[str] = set()
    opaque: list[str] = []
    for path in sorted(GATEWAY.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee(node)
            if name in _CODE_FIRST:
                arg = node.args[0] if node.args else None
            elif name in _CODE_SECOND:
                arg = node.args[1] if len(node.args) > 1 else None
            elif name == "require":
                # `Principal.require` builds its code from the capability;
                # the capabilities come from here.
                for a in node.args[:1]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        codes.add(f"{a.value}{_GRANT_SUFFIX}")
                    else:
                        opaque.append(f"{path.name}:{node.lineno} require(...)")
                continue
            else:
                continue
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                codes.add(arg.value)
            elif isinstance(arg, (ast.Name, ast.Attribute)):
                continue  # forwarded, counted where it was produced
            elif _derived_grant_code(arg):
                continue  # followed through the `require(...)` sites above
            elif arg is not None:
                opaque.append(f"{path.name}:{node.lineno} {name}(...)")

    # The redaction walk raises `_Refusal` and looks its message up by
    # code, so its registry is a producible-code list of its own.
    redaction = ast.parse((GATEWAY / "redaction.py").read_text())
    for node in ast.walk(redaction):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_MESSAGES" for t in node.targets
        ):
            for key in getattr(node.value, "keys", []):
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    codes.add(key.value)
    return codes, opaque


def _documented_codes() -> set[str]:
    """The FIRST column of §10 — prose elsewhere in a row backticks
    plenty of things that are not codes."""
    body = DOC.read_text().split("## 10. The refusals, in one place", 1)[1]
    rows = set()
    for line in body.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        first = line.split("|")[1]
        found = re.findall(r"`([a-z_]+)`", first)
        if found:
            rows.update(found)
    return rows


def test_every_refusal_the_gateway_can_raise_is_documented():
    codes, _ = _raised_codes()
    missing = sorted(codes - _documented_codes())
    assert not missing, (
        "these refusal codes are raised by the gateway and carry no row in "
        f"§10 of {DOC.name}, so an author meeting one has nothing to read: "
        f"{missing}"
    )


def test_every_documented_refusal_can_actually_be_raised():
    codes, _ = _raised_codes()
    phantom = sorted(_documented_codes() - codes)
    assert not phantom, (
        "§10 documents these refusal codes and no gateway module can produce "
        f"them, which a reader cannot tell from a real one: {phantom}"
    )


def test_no_refusal_hides_behind_a_code_this_walk_cannot_read():
    """The guard's own blind spot, made loud.

    A refusal whose code is computed would be invisible to the two tests
    above — they would pass by not looking, which is the failure mode
    this batch keeps finding. `require()` is followed deliberately; a
    second computed code means teaching this walk to follow it too, or
    deciding it should be a literal."""
    _, opaque = _raised_codes()
    assert not opaque, (
        "a refusal code here is not a literal, so the catalogue check "
        f"cannot see what it produces: {opaque}"
    )


@pytest.mark.parametrize("code", ["invocation_deadline", "invalid_json"])
def test_the_codes_this_batch_added_are_in_the_table(code):
    """Named, because these are the ones that were missing — a guard is
    easiest to believe when it fails for the case that prompted it."""
    assert code in _documented_codes()
