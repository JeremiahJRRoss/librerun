#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Find route-handler comprehensions whose ELEMENT EXPRESSION no test ever
evaluates.

The defect this exists for, from §12 207(v): `GET /runs/{id}/progress`
built its rows with ``[_step(k, v) for k, v in raw.items()]`` and `_step`
was deleted by a bad revert. That is a `NameError` — a 500 on every run
with any progress entry — and the full backend suite passed 1232 with it
in the tree. The only test driving that endpoint asserted the progress
hash was EMPTY, so the comprehension never evaluated its element and a
missing function was never reached. **A test for the absence of something
certifies the path it never enters.**

Line coverage cannot see this: the comprehension is one line, and that
line executes whether or not the collection has members. Nor can a green
suite — the test PASSED. So the check is a mutation. Each element
expression is replaced by two calls: one that RECORDS that it evaluated,
and one to a name that genuinely does not exist. Then the suite is run,
and two independent things are read back.

    marked and the suite raises NameError for it -> covered
    marked, and nothing failed                   -> a test ENTERS this
                                                    element and would not
                                                    notice it breaking
    not marked, and nothing failed               -> NO TEST ENTERS it
    not marked, and the suite failed for some
      other reason                               -> this run decides
                                                    nothing

The marker is not decoration. Without it, a handler that catches the
exception — or a test content with the degraded answer — is an element
expression that runs on every call and gets reported as one no test
enters. Measured on a handler wrapping its comprehension in
`try/except`: the guard called it unevaluated while a test drove it over
two rows (Codex round 1, P2).

Reading WHY the suite went red is the other half. The first mutation run
built here went red against a database one migration behind —
`UndefinedColumnError`, exit 1, nothing to do with the mutation — and a
probe that only read the exit code would have recorded "covered" for an
element expression no test had entered. The same rule applies to the
verdict at the end: a run that fails for an unrelated reason and does
not reach the probe reports UNDECIDED, never a finding (Codex round 1,
P1).

Mutating tests whether a BREAK WOULD BE NOTICED, which is a stronger
property than whether the line was reached and the one the founding
defect is about: `_step` was deleted and 1232 tests stayed green. The
marker separates the two questions instead of conflating them.

## Cost

Every candidate is mutated AT ONCE and the suite is run once. A
`NameError` naming probe 7 proves probe 7's element evaluated, whatever
the others did, so one run settles every candidate it names.
Only the ones it does NOT name are re-run alone, because a batch can
hide a candidate two ways: a comprehension nested inside another's
element is replaced along with it, and a test that fails at a mutated
handler never reaches the later one it would also have exercised.
Nesting is handled by batching on containment depth — two candidates at
the same depth cannot contain one another — and the second is what the
per-candidate phase is for. On a tree with nothing to find that is one
control run, one per depth and one per candidate hidden the second way.

A batch can also fail for a reason none of its members has: two walrus
elements binding the same name that a `nonlocal` refers to are each
mutable alone and not together. That batch stands down and its members
go through the per-candidate phase, so the answer costs more runs and is
still an answer (Codex round 2).

Usage:
    python3 scripts/element_expression_coverage.py [--roots DIR ...] [--list]

This guard, what it found and what it does not look at: §12 210, gap H14.

Exit codes:
    0  every candidate's element expression is evaluated by the suite
    1  at least one is entered by nothing, or entered and unnoticed
    5  something could not be measured: no tests collected, the control
       was not green, a run ended in a way that decides nothing, or an
       element this guard cannot mutate without making the module
       uncompilable
    8  the working tree already differs from HEAD in a target file
"""
from __future__ import annotations

import argparse
import ast
import os
import pathlib
import re
import subprocess
import tempfile
import sys
from dataclasses import dataclass

# Every attribute on FastAPI's `APIRouter` that REGISTERS a route when
# used as `@router.<name>(...)`. Five of these were here and seven were
# not, so a handler declared with `@router.api_route(..., methods=[...])`,
# `@router.options`, `@router.head`, `@router.trace`, `@router.route`,
# `@router.websocket` or `@router.websocket_route` was not a route
# handler as far as this guard was concerned: its element expressions
# bypassed the gate entirely while the commit adding it still triggered
# the job (Codex round 21, who named three of the seven; `websocket` and
# the two Starlette-style spellings came out of measuring the rest).
#
# MEASURED, not recalled: each public attribute of `APIRouter` was used
# as a decorator against a fresh router and the twelve that grew
# `router.routes` are these. `test_the_guard_knows_every_route_decorator`
# re-derives that list from the INSTALLED FastAPI and fails when this set
# no longer covers it, so the names cannot rot silently the way a
# hand-kept list does -- the defect this PR has now recorded four times.
# The set is a literal here because this script is stdlib-only by
# design; the test lives where FastAPI is always importable.
#
# On THIS tree the widening changes nothing: `backend/app/routers` uses
# `get`, `post`, `put` and `delete` only, and the census is the same 19
# element expressions with the same 14 / 1 / 4 split. It is insurance
# against a tree this one is not, and is labelled as such rather than
# credited with work it does not do.
ROUTE_DECORATOR_ATTRS = {
    "api_route", "delete", "get", "head", "options", "patch", "post",
    "put", "route", "trace", "websocket", "websocket_route",
}

# FastAPI also registers a route IMPERATIVELY, with an ordinary
# function passed to one of these: `router.add_api_route("/x", handler,
# methods=["GET"])` produces exactly the same route as the decorator
# and was invisible to this scan, so every element expression in such a
# handler bypassed the gate while the commit adding it still triggered
# the job (Codex round 22). Round 21 had found the decorator half of
# the same hole and recorded this half as a limitation instead of
# closing it -- the rule applied where the report pointed and not
# everywhere it is true, for the fifth time in this work.
#
# MEASURED like the decorators: these are the `APIRouter` methods whose
# signature takes an `endpoint`, and in all four it is the parameter
# AFTER `path` -- so the callable is `args[1]` positionally or the
# `endpoint=` keyword. `test_the_guard_knows_every_imperative_registration`
# re-derives both the names and the position from the installed FastAPI.
# `add_event_handler` is deliberately absent: it registers a lifecycle
# hook, not a route.
ROUTE_REGISTRATION_ATTRS = {
    "add_api_route", "add_api_websocket_route", "add_route",
    "add_websocket_route",
}
ROUTE_ENDPOINT_POSITION = 1
# No `-x`. A run that stops at the first failure stops before the tests
# that would have caught the other mutations, and would report them
# uncovered. `--tb=line` keeps eighteen simultaneous failures to eighteen
# lines while still printing each exception's own message, which is the
# only part being read.
SUITE = [sys.executable, "-m", "pytest", "-q", "--tb=line"]
SUITE_CWD = "backend"
PROBE_PREFIX = "_ELEMENT_EXPRESSION_PROBE_"
MARK_FN = "_lr_element_probe_mark"
MARK_ENV = "LR_ELEMENT_PROBE_FILE"

# Appended to every file a batch mutates, and removed with the rest of
# the mutation. It records that the element expression EVALUATED, which
# the NameError beside it cannot: a handler that catches the exception,
# or a test that accepts the degraded answer, swallows the notice while
# the body ran perfectly well. `import os` sits inside the function so
# the file gains no module-level import for a purity check to trip on.
MARK_HELPER = f'''


def {MARK_FN}(_probe, _seen=set()):
    if _probe not in _seen:
        _seen.add(_probe)
        import os

        _path = os.environ.get("{MARK_ENV}")
        if _path:
            with open(_path, "a") as _fh:
                _fh.write(_probe + "\\n")
    return None
'''

Pos = tuple[int, int]


@dataclass(frozen=True)
class Candidate:
    index: int
    path: pathlib.Path
    handler: str
    kind: str
    comp_line: int
    start: Pos  # (1-based line, 0-based BYTE column) of the element
    end: Pos
    source: str
    raw: bytes  # exactly the bytes at that range when it was scanned

    @property
    def probe(self) -> str:
        return f"{PROBE_PREFIX}{self.index}"

    def __str__(self) -> str:
        return (
            f"{self.path}:{self.comp_line} {self.handler}() "
            f"{self.kind} element {self.source}"
        )

    def contains(self, other: "Candidate") -> bool:
        """Is `other`'s element expression INSIDE this one's byte range?

        Written first as one chained tuple comparison, which compared
        the two starts and returned on that alone — so every candidate
        earlier in a file "contained" every later one and six sequential
        comprehensions in `admin.py` came out six batches deep. Two
        conditions, spelled as two conditions.
        """
        if self.path != other.path or self is other:
            return False
        return self.start <= other.start and other.end <= self.end


def _route_verb(fn: ast.AST) -> str | None:
    for deco in getattr(fn, "decorator_list", []):
        target = deco.func if isinstance(deco, ast.Call) else deco
        if isinstance(target, ast.Attribute) and target.attr in ROUTE_DECORATOR_ATTRS:
            return target.attr
    return None


def _imperatively_registered(tree: ast.AST) -> set[str]:
    """Names this module hands to a route-registering call.

    Only a plain name counts. `router.add_api_route("/x", make())` or
    `...(handlers[0])` registers something this scan cannot follow to a
    definition, and inventing a handler for it would be a guess; the
    limitation is stated in the pull request rather than papered over.
    """
    names: set[str] = set()
    for call in ast.walk(tree):
        if not isinstance(call, ast.Call):
            continue
        target = call.func
        if not isinstance(target, ast.Attribute):
            continue
        if target.attr not in ROUTE_REGISTRATION_ATTRS:
            continue
        endpoint = None
        for keyword in call.keywords:
            if keyword.arg == "endpoint":
                endpoint = keyword.value
        if endpoint is None and len(call.args) > ROUTE_ENDPOINT_POSITION:
            endpoint = call.args[ROUTE_ENDPOINT_POSITION]
        if isinstance(endpoint, ast.Name):
            names.add(endpoint.id)
    return names


def _element_of(comp: ast.AST) -> ast.expr:
    """The expression evaluated once per member.

    A dict comprehension evaluates key and value together, so mutating
    the value is enough to decide whether the body ran.
    """
    return comp.value if isinstance(comp, ast.DictComp) else comp.elt


def find_candidates(roots: list[pathlib.Path]) -> list[Candidate]:
    """Every comprehension in a route handler's BODY.

    Not its decorators: a comprehension in a decorator is evaluated when
    the module is imported, which every test does, so it would be
    reported covered by the act of collecting the suite.

    An earlier version of this scan collected only calls to functions
    DEFINED IN THE SAME FILE, and inside that, only names bound at module
    level — so it could not see `_step`, which is defined inside its own
    handler: a checker blind to its founding case. Measured across
    `backend/app/routers`, the same-file rule left 1 candidate of the 20
    calls appearing in an element expression, and the module-level rule
    left 1 of 2 comprehensions in `runs.py`. The rule is now the shape
    of the hazard — an element expression — and not the shape of the one
    example of it.
    """
    found: list[Candidate] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            # ONE read, and no newline translation, because `raw` is
            # compared against `read_bytes()` at splice time and the two
            # must be the same bytes.
            #
            # `read_text` performs universal-newline translation, so on a
            # checkout with CRLF endings -- `core.autocrlf=true`, the
            # Windows default, under this repo's `* text=auto` -- a
            # MULTI-LINE element recorded `\n` while the file held
            # `\r\n`, `_mutated_bytes` called that "the file changed
            # under the run", and the guard exited 5 with no verdict
            # about anything (Codex round 27). Single-line elements were
            # unaffected: the `\r` sits past the element's end column.
            #
            # Taking `raw` from a SECOND read instead is the smaller
            # diff and is wrong: measured, a save landing between the
            # two reads records the NEW bytes as `raw`, the comparison
            # then succeeds, and the splice lands in changed code --
            # which is precisely the hazard `raw` was added in round 24
            # to catch. One snapshot; both facts derived from it.
            data = path.read_bytes()
            source = data.decode("utf-8")
            tree = ast.parse(source)
            registered = _imperatively_registered(tree)
            for fn in ast.walk(tree):
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _route_verb(fn) is None and fn.name not in registered:
                    continue
                for stmt in fn.body:
                    for comp in ast.walk(stmt):
                        if not isinstance(
                            comp,
                            (ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp),
                        ):
                            continue
                        element = _element_of(comp)
                        text = ast.get_source_segment(source, element) or "?"
                        start = (element.lineno, element.col_offset)
                        end = (element.end_lineno, element.end_col_offset)
                        found.append(
                            Candidate(
                                index=len(found),
                                path=path,
                                handler=fn.name,
                                kind=type(comp).__name__,
                                comp_line=comp.lineno,
                                start=start,
                                end=end,
                                source=_one_line(text),
                                raw=_extract(data, start, end),
                            )
                        )
    return _one_candidate_per_span(found)


def _one_candidate_per_span(found: list[Candidate]) -> list[Candidate]:
    """One candidate per (file, byte range), innermost handler winning.

    A decorated route handler defined INSIDE another is scanned twice:
    `ast.walk` reaches the inner comprehension from the outer handler's
    body, and the inner handler's own scan reaches it again. Two
    candidates then carry the same range, land in the same batch
    because neither contains the other, and the second splice reads the
    bytes the first one wrote -- so `_mutated_bytes` raised
    `AssertionError` and the job died without a verdict (Codex round
    18). Worse, it died saying *the file changed under the run*, which
    is the one thing that had not happened; a reader would have gone
    looking for a concurrent edit.

    Descending is kept and the duplicate removed, rather than refusing
    to enter nested definitions: a comprehension inside a plain nested
    helper is still run by the route that calls it, and not entering
    would stop measuring it at all -- losing a check to fix a crash.
    The LAST attribution wins because `ast.walk` is breadth-first, so
    the inner handler -- the one that actually runs the expression --
    is seen after the outer one.

    Indices are handed out here rather than at append time, because
    `batches` keys containment depth by index and a gap or a repeat
    there would silently mis-group.
    """
    import dataclasses

    by_span: dict[tuple[pathlib.Path, tuple[int, int], tuple[int, int]], Candidate] = {}
    for cand in found:
        by_span[(cand.path, cand.start, cand.end)] = cand
    return [
        dataclasses.replace(cand, index=position)
        for position, cand in enumerate(by_span.values())
    ]


def _one_line(text: str, width: int = 56) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def batches(candidates: list[Candidate]) -> list[list[Candidate]]:
    """Group by containment depth.

    Two candidates at the same depth cannot contain one another, so every
    batch is a set of disjoint byte ranges that can be mutated together.
    `list_agent_keys` is the case this exists for: its dict comprehension
    sits inside the element expression of the list comprehension around
    it, and replacing the outer one deletes the inner.
    """
    depth = {
        c.index: sum(1 for other in candidates if other.contains(c)) for c in candidates
    }
    ordered: dict[int, list[Candidate]] = {}
    for cand in candidates:
        ordered.setdefault(depth[cand.index], []).append(cand)
    return [ordered[d] for d in sorted(ordered)]


def _tree_is_clean(paths: set[pathlib.Path]) -> tuple[bool, str]:
    """Are the files about to be mutated identical to HEAD?

    A failed `git diff` is NOT a clean tree. Read as `stdout is empty`,
    this returned "clean" whenever git could not answer at all — not a
    repository, no HEAD, git absent — so the one preflight standing
    between a crashed earlier run's leftover mutation and a "covered"
    verdict passed by not looking. The exit code decides.

    Neither is an UNTRACKED file. `git diff HEAD -- <path>` exits 0 with
    no output for a path that has no version in `HEAD` at all, so a
    newly written router passed as clean — and so did the same file
    holding a mutation an interrupted run had left in it, which is
    precisely the state this preflight exists to refuse. It would then
    be spliced on top of, and the verdict would describe code nobody
    wrote (Codex round 19). `git ls-files --error-unmatch` answers the
    question the diff cannot, and it is worth stating it exactly: is
    this path TRACKED? Not "is it in HEAD" — a file `git rm --cached`
    removed from the index is in HEAD and fails this, and a file just
    `git add`ed is in neither HEAD nor a commit yet passes it. Tracked
    is the property that makes the diff's silence meaningful, so it is
    the one asked for and the one reported.

    Third defect in this one function, which is worth saying plainly:
    it is eight lines long and has now been wrong about a failed git,
    an untracked file, and what "no output" means twice.
    """
    proc = subprocess.run(
        ["git", "diff", "--name-only", "HEAD", "--", *map(str, sorted(paths))],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return False, (
            "git could not report the state of these files "
            f"(git diff exited {proc.returncode}): {proc.stderr.strip()}\n"
            "Run this inside a git repository that has a HEAD to compare "
            "against."
        )
    # Asked SECOND, so that a git which cannot answer at all still
    # reports round 1's reason rather than this one. The diff having
    # succeeded, empty output now means one of two things, and only
    # this tells them apart.
    known = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", *map(str, sorted(paths))],
        capture_output=True,
        text=True,
    )
    if known.returncode != 0:
        return False, (
            "these files are not tracked, so there is no recorded version "
            "for the diff against HEAD to be silent ABOUT, and nothing can "
            f"say whether they hold a leftover mutation: {known.stderr.strip()}\n"
            "Commit them (or drop them from the candidate set) and re-run. "
            "`git checkout --` cannot restore a file git has never seen, "
            "which is why the remedy is named per reason and not once for "
            "all of them."
        )
    dirty = [line for line in proc.stdout.splitlines() if line.strip()]
    if dirty:
        return False, (
            "\n".join(dirty)
            + "\nRestore them (git checkout -- <path>) and re-run."
        )
    return True, ""


def _run_suite(marker: pathlib.Path | None = None) -> tuple[int, str, set[str]]:
    """Run the suite; return its exit code, its output, and which probes
    recorded that they EVALUATED.

    The marker file is the independent observation. The NameError in the
    output says a test NOTICED the break; the marker says the body ran,
    whether or not anything noticed. They are different questions and
    the guard was answering only one of them.
    """
    env = dict(os.environ)
    if marker is not None:
        marker.write_text("")
        env[MARK_ENV] = str(marker.resolve())
    else:
        env.pop(MARK_ENV, None)
    proc = subprocess.run(
        SUITE, cwd=SUITE_CWD, capture_output=True, text=True, env=env
    )
    marked: set[str] = set()
    if marker is not None and marker.exists():
        marked = {line.strip() for line in marker.read_text().splitlines() if line.strip()}
    return proc.returncode, proc.stdout + proc.stderr, marked


def _evaluated(cand: Candidate, output: str) -> bool:
    """Did the suite go red BECAUSE this element expression evaluated?

    Measured, not assumed. With the element replaced, a covering test
    fails with exactly::

        NameError: name '_ELEMENT_EXPRESSION_PROBE_13' is not defined

    Requiring that exact evidence is the point. A red suite on its own
    means nothing, and a red suite that names a DIFFERENT probe means
    nothing about this one. It also separates a runtime evaluation from
    a static check that merely spots an unknown name: a linter's
    complaint is not a test reaching the code.
    """
    return f"NameError: name '{cand.probe}' is not defined" in output


def _decides_nothing(code: int) -> str | None:
    """Exit codes that are not a verdict.

    0 (nothing failed) and 1 (something did) are both readings. 5 is a
    suite that collected no tests, and 2/3/4 are interruption, internal
    error and usage error — a probe that mapped every non-zero exit to
    "caught" would read all three as a test catching the mutation, which
    is the defect this guard exists to find, one level up.
    """
    if code == 5:
        return "the suite collected no tests"
    if code not in (0, 1):
        return f"pytest exited {code}: not a test result"
    return None


def _splice(data: bytes, start: Pos, end: Pos, replacement: bytes) -> bytes:
    lines = data.split(b"\n")
    (sl, sc), (el, ec) = start, end
    head, tail = lines[sl - 1][:sc], lines[el - 1][ec:]
    return b"\n".join(lines[: sl - 1] + [head + replacement + tail] + lines[el:])


def _extract(data: bytes, start: Pos, end: Pos) -> bytes:
    lines = data.split(b"\n")
    (sl, sc), (el, ec) = start, end
    if sl == el:
        return lines[sl - 1][sc:ec]
    return b"\n".join([lines[sl - 1][sc:]] + lines[sl : el - 1] + [lines[el - 1][:ec]])


def _replacement(cand: Candidate) -> bytes:
    """What the element expression becomes.

    Two things in one expression: a call that RECORDS the evaluation and
    a call to a name that genuinely does not exist. The tuple's `[1]`
    keeps the undefined call in value position, so the exception is a
    real `NameError` for a real missing name — the same failure a
    deleted helper produces — rather than one this script synthesises.
    """
    return f'({MARK_FN}("{cand.probe}"), {cand.probe}())[1]'.encode("utf-8")


def _mutated_bytes(data: bytes, cands: list[Candidate]) -> bytes:
    """Apply every candidate's replacement to one file's bytes.

    Edits run LAST FIRST, so an earlier edit never moves a later one's
    offsets. Before each edit the bytes at the recorded range are parsed
    on their own: if they are not the expression the AST said they were,
    the positions and the file disagree and nothing is written.
    """
    for cand in sorted(cands, key=lambda c: c.start, reverse=True):
        segment = _extract(data, cand.start, cand.end)
        if segment != cand.raw:
            # THE EXACT BYTES, not "some expression parses here". Read as
            # a parse, a range gone stale — the file edited after the
            # scan, which the four-minute control run leaves plenty of
            # room for — was accepted whenever it happened to cover
            # another valid expression, and the checker mutated
            # unrelated code and reported a coverage verdict about it.
            # Measured: with `render(v)` replaced by `DIFFERENT(v)`
            # between the scan and the splice, the parse accepted the
            # nine bytes now reading `DIFFERENT` and spliced there.
            # Parenthesising made it looser still: `x for x in values`
            # is a SyntaxError alone and parses wrapped (Codex round 2).
            raise AssertionError(
                f"{cand.path}:{cand.start[0]} holds {segment!r}, not the "
                f"{cand.raw!r} scanned there — the file changed under the run"
            )
        data = _splice(data, cand.start, cand.end, _replacement(cand))
    return data + MARK_HELPER.encode("utf-8")


def compile_error(cand: Candidate) -> str | None:
    """Would mutating this one candidate produce a module Python refuses?

    `ast.parse` is a parser; it builds no symbol table. An element like
    ``(x := v)`` binds a name in the enclosing function, and a handler
    that later declares ``nonlocal x`` in an inner function stops
    compiling the moment that binding goes — `no binding for nonlocal
    'x' found`, raised by `compile` and not by `ast.parse`. Written to
    the file, that turns the gate into something that rejects every
    change to a file holding a perfectly valid comprehension.

    Screened here, before anything is written, so such a candidate is
    reported as one this guard cannot measure instead of crashing the
    run or being silently called uncovered.
    """
    try:
        mutated = _mutated_bytes(cand.path.read_bytes(), [cand])
        compile(mutated.decode("utf-8"), str(cand.path), "exec")
    except SyntaxError as exc:
        return f"{type(exc).__name__}: {exc.msg}"
    except AssertionError as exc:
        # The splice itself refused. Reported rather than raised: one
        # element this guard cannot touch must not stop it measuring the
        # rest, and must not be counted either way.
        return str(exc)
    return None


class BatchNotCompilable(Exception):
    """A GROUP of mutations leaves a module that will not compile, even
    though each of them alone does.

    Screening candidates one at a time does not settle this. Two
    comprehensions in one handler can each bind `x` with `(x := v)`
    while an inner function declares `nonlocal x`: mutate either alone
    and the other binding still stands, mutate both and neither does.
    Reproduced exactly that way — the compile inside `mutate` raised,
    the exception escaped `main`, and a valid router module crashed the
    gate rather than being measured (Codex round 2).

    Raised instead, so the batch can fall back to its members one at a
    time. Each has been screened, so each compiles, and the candidates
    are measured rather than lost.
    """


def mutate(
    group: list[Candidate],
) -> tuple[dict[pathlib.Path, bytes], dict[pathlib.Path, bytes]]:
    """Replace each element expression.

    Returns the original bytes of every file touched AND the bytes this
    run wrote into it. `restore` needs both: the original to put back,
    and what it wrote to recognise a file that is still its own. Without
    the second, restoring is indistinguishable from overwriting whatever
    the developer saved in the four minutes the suite takes (round 26).
    """
    originals: dict[pathlib.Path, bytes] = {}
    written: dict[pathlib.Path, bytes] = {}
    by_file: dict[pathlib.Path, list[Candidate]] = {}
    for cand in group:
        by_file.setdefault(cand.path, []).append(cand)

    try:
        for path, cands in by_file.items():
            data = originals[path] = path.read_bytes()
            mutated = _mutated_bytes(data, cands)
            # `compile`, not `ast.parse`: see compile_error above.
            try:
                compile(mutated.decode("utf-8"), str(path), "exec")
            except SyntaxError as exc:
                raise BatchNotCompilable(
                    f"{path}: mutating {len(cands)} element expression(s) "
                    f"together leaves a module that will not compile "
                    f"({exc.msg}); each of them alone does"
                ) from exc
            path.write_bytes(mutated)
            written[path] = mutated
    except BaseException:
        # What this run wrote is passed too: an abort part-way through
        # must not clobber a file a developer has since saved either.
        restore(originals, written)
        raise
    return originals, written


class RestoreConflict(Exception):
    """A file changed under the run, so its original was not put back."""


def restore(
    originals: dict[pathlib.Path, bytes],
    written: dict[pathlib.Path, bytes] | None = None,
) -> None:
    r"""Put the originals back — unless the file is no longer ours.

    The control run takes four minutes and the mutated tree is the
    developer's real checkout, so an edit saved while pytest is running
    is not hypothetical. This used to write the pre-run bytes back
    unconditionally, which silently destroyed that edit: the preflight
    and the `raw` comparison both look BEFORE the mutation, and nothing
    looked after it (Codex round 26).

    So each file is only restored when it still holds exactly what this
    run wrote into it. One that does not is left alone and reported, and
    the caller is told which — losing a mutation the developer can see
    and undo beats losing work they cannot get back.

    `written` is optional so an interrupted call that never got as far
    as recording what it wrote still restores what it can; with nothing
    to compare against, the old behaviour is the only one available and
    the file is put back.
    """
    conflicts = []
    for path, data in originals.items():
        expected = (written or {}).get(path)
        if expected is not None:
            try:
                now = path.read_bytes()
            except OSError as exc:
                conflicts.append(f"{path}: cannot be read back ({exc})")
                continue
            if now != expected:
                conflicts.append(
                    f"{path}: holds neither the mutation this run wrote nor "
                    f"anything this run can account for — it was edited while "
                    f"the suite was running. The original is NOT written back; "
                    f"it is {len(data)} bytes and the file now has {len(now)}."
                )
                continue
        path.write_bytes(data)
        if path.read_bytes() != data:
            conflicts.append(f"{path}: failed to restore")
    if conflicts:
        raise RestoreConflict(
            "the tree changed under this run:\n  " + "\n  ".join(conflicts)
        )


def _summary_line(output: str) -> str:
    """pytest's own last line: ``1274 passed, 12 skipped in 271.44s``.

    Printed with the control verdict because a green suite and a suite
    that did not run are the same exit code. A test that SKIPS cannot
    catch a mutation, so a substrate where the database-backed tests
    skipped would report every element expression uncovered — a loud
    wrong answer rather than a silent one, but only if the reader can
    see the skips. Reported, not thresholded: what counts as too many
    skips is a judgement no number here would settle.
    """
    for line in reversed(output.strip().splitlines()):
        if re.search(r"\d+ (passed|failed|error|skipped|no tests ran)", line):
            return line.strip("= ").strip()
    return "(pytest printed no summary line)"


def _failures(output: str, limit: int = 24, indent: str = "") -> str:
    """The lines that say what failed, wherever pytest put them.

    `--tb=line` prints one line per failure in the FAILURES section and
    the bare test ids again at the end, and between them sit the warnings
    summary — so a fixed tail of the output shows which test failed and
    not why. Both kinds of line are kept, in the order they appeared.
    """
    keep = [
        line
        for line in output.splitlines()
        if line.startswith(("FAILED ", "ERROR "))
        or re.search(r"\.py:\d+: \w*(Error|Exception)", line)
    ]
    if not keep:
        return _tail(output, indent=indent)
    shown = keep[:limit]
    if len(keep) > limit:
        shown.append(f"… and {len(keep) - limit} more")
    return "\n".join(indent + line for line in shown)


def _tail(output: str, lines: int = 12, indent: str = "") -> str:
    """The last few lines of a suite run, so a verdict of "this decides
    nothing" shows its own evidence instead of asking the reader to
    reproduce it."""
    tail = output.strip().splitlines()[-lines:]
    return "\n".join(indent + line for line in tail)


def main() -> int:
    # Each suite run is minutes long and the whole point of the progress
    # lines is watching them arrive. Piped to a file or a CI log, stdout
    # is block-buffered, so without this the lines appear in batches of
    # four kilobytes and the timestamps beside them in a CI log belong to
    # the flush rather than the event.
    sys.stdout.reconfigure(line_buffering=True)

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--roots",
        nargs="+",
        default=["backend/app/routers"],
        help="directories to scan for route handlers",
    )
    ap.add_argument("--list", action="store_true", help="list candidates and exit")
    args = ap.parse_args()

    # A root that is not there is not an empty root. `Path.rglob` is
    # SILENT about a missing directory -- measured, it yields nothing
    # exactly as a real-but-empty one does -- so renaming
    # `backend/app/routers` made this print "no candidates" and exit 0:
    # the gate reporting success having looked at nothing, which is the
    # failure it exists to catch, one level up. The deletion triggers
    # the workflow once; every change under the new location then goes
    # unguarded while the job stays green (Codex round 26).
    #
    # The invariant is checked HERE, where the roots are turned into
    # paths, for the reason `_workflow_files` gives about the workflow
    # census: at the single collector, not at each consumer.
    roots = [pathlib.Path(r) for r in args.roots]
    absent = [r for r in roots if not r.is_dir()]
    if absent:
        print(
            "REFUSING: these scan roots are not directories: "
            + ", ".join(str(r) for r in absent)
            + ". An empty scan and a missing one look identical to "
            "`rglob`, so this cannot be reported as a clean run. Either "
            "the path moved — update --roots and the workflow's paths "
            "filter together — or the checkout is incomplete.",
            file=sys.stderr,
        )
        return 5

    candidates = find_candidates(roots)
    if not candidates:
        # Now this means what it says: the roots are real and hold no
        # comprehension in any route handler.
        print(f"no candidates: no route handler under "
              f"{', '.join(str(r) for r in roots)} builds a comprehension.")
        return 0

    undecidable: list[tuple[Candidate, str]] = []
    measurable: list[Candidate] = []
    for cand in candidates:
        why = compile_error(cand)
        (undecidable.append((cand, why)) if why else measurable.append(cand))

    groups = batches(measurable)
    print(f"{len(candidates)} element expression(s) in {len(groups)} batch(es):")
    for depth, group in enumerate(groups):
        for cand in group:
            print(f"  [{cand.probe}] depth {depth}  {cand}")
    for cand, why in undecidable:
        print(f"  [{cand.probe}] CANNOT MEASURE  {cand}\n      {why}")
    if args.list:
        return 0
    if not measurable:
        print("\nnothing measurable to run.", file=sys.stderr)
        return 5

    clean, dirty = _tree_is_clean({c.path for c in candidates})
    if not clean:
        print(
            "\nREFUSING TO RUN: these target files are not in a state this run\n"
            "can mutate safely, so a mutation left by an earlier crashed run\n"
            f"could be mistaken for the code under test:\n{dirty}",
            file=sys.stderr,
        )
        return 8

    marker = pathlib.Path(tempfile.mkdtemp(prefix="lr-element-probe-")) / "marks"

    print("\ncontrol run (the suite must be green before any mutation means anything)")
    code, control_out, _ = _run_suite(marker)
    # The same reading as a mutation run's, because the failures are the
    # same failures: a control that collected nothing is not a control
    # that passed, and saying only "not green" of it would describe the
    # one case where the suite never ran as the case where it ran and
    # objected.
    why = _decides_nothing(code)
    if why is not None:
        print(f"INVALID: the control run decides nothing — {why}.\n{_tail(control_out)}",
              file=sys.stderr)  # a suite that ran no tests has no failure to quote
        return 5
    if code != 0:
        print(
            f"INVALID: the control run was not green (pytest exit {code}).\n"
            "Whatever is already failing fails again under every mutation, and\n"
            "no reading here can separate the two. Measured both ways on a\n"
            "substrate with one unrelated failing test: the run reported every\n"
            "element expression covered and exited 0, on a suite that was red\n"
            "throughout; and had the failing test been the covering one, the\n"
            "same run would have reported a finding against code a test\n"
            "exercises. Nothing is mutated until this is green.\n"
            f"{_failures(control_out)}",
            file=sys.stderr,
        )
        return 5
    print(f"control: green — {_summary_line(control_out)}")

    pending = list(measurable)
    covered: list[Candidate] = []
    unnoticed: list[Candidate] = []
    unentered: list[Candidate] = []
    undecided: list[tuple[Candidate, str]] = []

    for depth, group in enumerate(groups):
        print(f"\nbatch: depth {depth}, {len(group)} element expression(s) at once")
        try:
            originals, written = mutate(group)
        except BatchNotCompilable as exc:
            # Not a failure of the tree and not a verdict: these go to
            # the per-candidate phase below, where each one compiles.
            print(f"  batch skipped — {exc}")
            print(f"  its {len(group)} candidate(s) are measured one at a time instead")
            continue
        try:
            code, output, _marked = _run_suite(marker)
        finally:
            restore(originals, written)
        why = _decides_nothing(code)
        if why is not None:
            print(f"INVALID: {why}\n{_failures(output)}", file=sys.stderr)
            return 5
        # A batch settles a candidate only when a test NOTICED it. A
        # marker without a notice is not final here: another mutation in
        # the same batch may have aborted the test that would have
        # noticed this one.
        for cand in list(pending):
            if _evaluated(cand, output):
                pending.remove(cand)
                covered.append(cand)
                print(f"  covered  [{cand.probe}] {cand.source}  (batch {depth})")

    # Everything the batches did not settle, alone — where nothing else
    # is mutated, so both signals are final.
    for cand in list(pending):
        print(f"\nalone: [{cand.probe}] {cand}")
        # `compile_error` screened this one on its own before any run, so
        # a BatchNotCompilable here would mean the file changed under us
        # — which the byte comparison in `_mutated_bytes` reports first.
        originals, written = mutate([cand])
        try:
            code, output, marked = _run_suite(marker)
        finally:
            restore(originals, written)
        why = _decides_nothing(code)
        if why is not None:
            print(f"INVALID: {why}\n{_failures(output)}", file=sys.stderr)
            return 5
        pending.remove(cand)
        if _evaluated(cand, output):
            covered.append(cand)
            print(f"  covered  [{cand.probe}] {cand.source}  (alone)")
        elif cand.probe in marked and code == 0:
            unnoticed.append(cand)
            print(
                f"  ENTERED BUT NOT NOTICED  [{cand.probe}] {cand.source} — a test"
                " runs this element and would not see it break"
            )
        elif cand.probe in marked:
            # Marked, and the suite went red WITHOUT naming this probe.
            # The truth table allows "would not notice" only when
            # nothing failed, and this branch used to reach it whatever
            # the exit code — so a handler that catches the injected
            # NameError while a test rejects the degraded answer was
            # reported as unnoticed, which is the opposite of what
            # happened: a test did notice (Codex round 12).
            #
            # Round 1 fixed exactly this for the UNMARKED branch below
            # and the rule was not carried to this one, which is the
            # shape §12 210 keeps recording: a rule applied where the
            # finding pointed and not everywhere it is true.
            undecided.append(
                (
                    cand,
                    f"the element evaluated, and the suite exited {code} without "
                    f"naming its probe — this run cannot tell a test that noticed "
                    f"some other way from a failure that has nothing to do with it",
                )
            )
            print(f"  UNDECIDED  [{cand.probe}] {cand.source}")
            print(_failures(output, indent="      "))
        elif code == 0:
            unentered.append(cand)
            print(f"  NEVER ENTERED  [{cand.probe}] {cand.source}")
        else:
            # Red, no notice, no marker. The suite objected to something
            # else, and this run cannot tell "nothing entered it" from
            # "the run never got that far". The rule this file states
            # about a red run proving nothing applies here too.
            undecided.append(
                (cand, f"the suite exited {code} without reaching or naming this probe")
            )
            print(f"  UNDECIDED  [{cand.probe}] {cand.source}")
            print(_failures(output, indent="      "))

    print("\n" + "=" * 68)

    if undecidable or undecided:
        for cand, why in undecidable:
            print(f"CANNOT MEASURE  {cand}\n    {why}", file=sys.stderr)
        for cand, why in undecided:
            print(f"UNDECIDED       {cand}\n    {why}", file=sys.stderr)
        print(
            "\nThese are not findings and not passes. The guard could not measure\n"
            "them, and reporting either verdict would be a claim this run does\n"
            "not carry.",
            file=sys.stderr,
        )
        return 5

    if unentered or unnoticed:
        if unentered:
            print("Element expressions NO TEST ENTERS:\n")
            for cand in unentered:
                print(f"  {cand}")
            print(
                "\nEach is a comprehension whose body never runs under the suite. A\n"
                "helper it calls could be deleted and the suite would stay green —\n"
                "which is how a 500 on every run with progress survived 1232 tests.\n"
                "Add a test that drives the endpoint with a NON-EMPTY collection."
            )
        if unnoticed:
            print("\nElement expressions a test ENTERS BUT WOULD NOT NOTICE BREAKING:\n")
            for cand in unnoticed:
                print(f"  {cand}")
            print(
                "\nThe marker says these ran and no test failed when the call inside\n"
                "them was replaced by one that does not exist. Something between\n"
                "the expression and the assertion absorbs the failure — an `except`\n"
                "in the handler, a test that accepts the degraded answer. The body\n"
                "is reached, which the empty-collection case is not, and it is\n"
                "still true that it could break silently."
            )
        print(
            f"\nThe control run was: {_summary_line(control_out)}\n"
            "A test that skipped cannot catch a mutation, so check that line\n"
            "before believing a finding: a substrate missing the database, or\n"
            "anything else these tests need, reports uncovered for the same\n"
            "reason it reports nothing at all."
        )
        return 1

    print(f"all {len(covered)} element expression(s) are evaluated by the suite")
    return 0


if __name__ == "__main__":
    sys.exit(main())
