"""The guard that finds unevaluated element expressions, tested itself.

`scripts/element_expression_coverage.py` mutates route handlers and runs
the whole backend suite, so its end-to-end behaviour cannot live in that
suite — it would be the suite running itself. What CAN live here is
every part of it that decides something without running pytest, and
those are the parts that were wrong while it was being written:

* containment, spelled first as one chained tuple comparison that
  compared the two starts and returned on that alone, which made six
  sequential comprehensions in `admin.py` come out six batches deep;
* the verdict, which must read WHY a suite went red — the first mutation
  run ever made with this went red on `UndefinedColumnError` against a
  database one migration behind, and an exit-code-only reading would
  have recorded "covered" for an element expression no test had entered;
* the exit codes that are not a verdict at all, 5 above all;
* the splice, which edits a byte range the AST named and must refuse
  when the bytes at that range are not what the AST said.

Nothing here touches a file in the repository. Every case builds its own
router module under `tmp_path`, because the checker's own control run
executes this suite: a test that mutated `app/routers/` would be editing
the tree out from under a run in progress.

The end-to-end cases — a handler driven over an empty collection, a
suite that collects nothing, a red control, a mutation hidden by
another, a target file git has never seen — live in a harness that
builds a throwaway tree and runs the real thing. How many scenarios and
assertions that is, is recorded in §12 210 beside the run that measured
it, and is deliberately NOT repeated here: this docstring carried "nine
scenarios, five injections" for several rounds after both had grown,
which is the second hand-kept copy of these numbers to go stale in two
rounds (Codex round 20; the gap row's injection count was the first).
A figure nothing here measures cannot be kept right here, so it is not
kept here. The harness itself is not in this suite because it needs a
Postgres and minutes a case.
"""
from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys

import pytest

_CHECKER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "element_expression_coverage.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("_eec_under_test", _CHECKER)
    module = importlib.util.module_from_spec(spec)
    # Registered before execution: a frozen dataclass resolves its
    # annotations through `sys.modules[cls.__module__]`, which is None
    # for a module loaded out of band.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


eec = _load()


ROUTER = '''\
from fastapi import APIRouter

router = APIRouter()

_ROWS: list[int] = []


def _to_row(value: int) -> dict:
    return {"value": value}


@router.get("/rows")
async def list_rows() -> dict:
    return {"rows": [_to_row(v) for v in _ROWS]}


@router.post("/rows")
async def add_rows() -> dict:
    return {"seen": {str(v): v * 2 for v in _ROWS}}


def helper_that_is_not_a_route() -> list:
    return [_to_row(v) for v in _ROWS]
'''


@pytest.fixture
def routers(tmp_path):
    root = tmp_path / "routers"
    root.mkdir()
    (root / "rows.py").write_text(ROUTER)
    return root


def test_only_route_handlers_are_candidates(routers):
    """A comprehension in an ordinary helper is not this guard's business.

    `helper_that_is_not_a_route` builds the same list as the handler. The
    scan is over route handlers because that is where the defect was and
    where the endpoint's own test is the thing that should have entered
    it; widening it to every function in the tree would be a different
    check with a different owner.
    """
    found = eec.find_candidates([routers])

    assert {c.handler for c in found} == {"list_rows", "add_rows"}


def test_a_dict_comprehension_is_found_through_its_value(routers):
    """Key and value evaluate together, so the value decides it."""
    found = {c.handler: c for c in eec.find_candidates([routers])}

    assert found["add_rows"].kind == "DictComp"
    assert found["add_rows"].source == "v * 2"


def test_containment_is_two_conditions_not_a_chained_comparison(tmp_path):
    """The bug that made six sequential comprehensions six batches deep.

    Two candidates in one file, disjoint, the second after the first.
    Neither contains the other, so both belong in the same batch — and
    the comparison that only looked at the starts said the first
    contained the second.
    """
    path = tmp_path / "x.py"
    first = eec.Candidate(0, path, "h", "ListComp", 1, (1, 4), (1, 9), "a", b"a")
    second = eec.Candidate(1, path, "h", "ListComp", 5, (5, 4), (5, 9), "b", b"b")

    assert not first.contains(second)
    assert not second.contains(first)
    assert len(eec.batches([first, second])) == 1


def test_a_nested_element_expression_lands_in_a_later_batch(tmp_path):
    """`list_agent_keys` in one sentence: the inner comprehension sits
    inside the outer one's element, so replacing the outer deletes the
    inner and a single batch could never decide it."""
    path = tmp_path / "x.py"
    outer = eec.Candidate(0, path, "h", "ListComp", 1, (1, 4), (3, 20), "outer", b"outer")
    inner = eec.Candidate(1, path, "h", "ListComp", 2, (2, 8), (2, 18), "inner", b"inner")

    assert outer.contains(inner)
    assert not inner.contains(outer)
    assert eec.batches([outer, inner]) == [[outer], [inner]]


def test_a_candidate_never_contains_itself(tmp_path):
    path = tmp_path / "x.py"
    one = eec.Candidate(0, path, "h", "ListComp", 1, (1, 4), (1, 9), "a", b"a")

    assert not one.contains(one)


def test_containment_does_not_reach_across_files(tmp_path):
    a = eec.Candidate(0, tmp_path / "a.py", "h", "ListComp", 1, (1, 0), (9, 0), "a", b"a")
    b = eec.Candidate(1, tmp_path / "b.py", "h", "ListComp", 2, (2, 0), (2, 5), "b", b"b")

    assert not a.contains(b)


def test_covered_requires_a_NameError_naming_THIS_probe(tmp_path):
    """The verdict, and the three ways a red suite says nothing.

    The `UndefinedColumnError` line is the real one, from the first
    mutation run ever made with this guard: exit 1, nothing to do with
    the mutation. The static-check line is a linter's complaint, which
    names the probe without any test having entered the code. The third
    names a different probe, which decides nothing about this one.
    """
    path = tmp_path / "x.py"
    cand = eec.Candidate(7, path, "h", "ListComp", 1, (1, 4), (1, 9), "a", b"a")
    probe = cand.probe

    assert eec._evaluated(cand, f"E   NameError: name '{probe}' is not defined")
    assert not eec._evaluated(cand, "E   sqlalchemy.exc.ProgrammingError: ...")
    assert not eec._evaluated(cand, f"E   AssertionError: assert '{probe}' not in src")
    other = eec.Candidate(8, path, "h", "ListComp", 1, (1, 4), (1, 9), "a", b"a")
    assert not eec._evaluated(cand, f"NameError: name '{other.probe}' is not defined")


@pytest.mark.parametrize("code", [2, 3, 4, 5, -11])
def test_every_exit_that_is_not_a_test_result_decides_nothing(code):
    """The defect the first probe carried: it mapped EVERY non-zero exit
    to "caught", so a suite that collected nothing looked like a suite
    that failed."""
    assert eec._decides_nothing(code) is not None


@pytest.mark.parametrize("code", [0, 1])
def test_a_pass_and_a_failure_are_both_readings(code):
    assert eec._decides_nothing(code) is None


def test_mutation_replaces_the_element_and_restores_the_bytes(routers):
    before = (routers / "rows.py").read_bytes()
    found = [c for c in eec.find_candidates([routers]) if c.handler == "list_rows"]

    originals, written = eec.mutate(found)
    try:
        mutated = (routers / "rows.py").read_text()
        assert f"{found[0].probe}()" in mutated
        # The fixture spells the SAME comprehension twice: once in the
        # handler and once in a plain helper. Exactly one survives, and
        # which one is the point — the first draft of this line asserted
        # the text was gone altogether and failed on the copy that is
        # not a route handler and must not be touched.
        assert mutated.count("[_to_row(v) for v in _ROWS]") == 1
        helper = mutated.split("def helper_that_is_not_a_route")[1]
        assert "[_to_row(v) for v in _ROWS]" in helper
        # Still a module: a mutation that cannot be imported would make
        # every test fail at collection and say nothing about coverage.
        compile(mutated, "rows.py", "exec")
    finally:
        eec.restore(originals, written)

    assert (routers / "rows.py").read_bytes() == before


def test_every_candidate_in_a_batch_is_mutated_at_once(routers):
    """One suite run has to settle the whole batch, which it cannot do
    if only one of them is replaced."""
    found = eec.batches(eec.find_candidates([routers]))[0]
    assert len(found) == 2

    originals, written = eec.mutate(found)
    try:
        mutated = (routers / "rows.py").read_text()
        assert all(f"{c.probe}()" in mutated for c in found)
    finally:
        eec.restore(originals, written)


def test_a_failed_git_diff_is_not_a_clean_tree(tmp_path, monkeypatch):
    """Read as "stdout was empty", the preflight called every tree clean
    that git could not answer about — not a repository, no HEAD, git
    absent — which is the one check standing between a crashed run's
    leftover mutation and a verdict about it."""
    monkeypatch.chdir(tmp_path)  # no .git here

    clean, why = eec._tree_is_clean({tmp_path / "x.py"})

    assert clean is False
    assert "git could not report the state" in why
    # Round 19 added a second refusal that a non-repository ALSO
    # triggers, so this pins which one answered: the diff is asked
    # first precisely so that git's inability keeps its own diagnosis.
    assert "not tracked" not in why


def test_the_replacement_both_records_and_raises(tmp_path):
    """Two calls, and each answers a different question.

    The undefined name is what a deleted helper produces, so the failure
    the suite sees is the real one. The marker beside it is the only
    thing that can tell "no test enters this" from "a test enters it and
    something swallows the exception" — which a handler wrapping its
    comprehension in `try/except` does on every call.
    """
    cand = eec.Candidate(3, tmp_path / "x.py", "h", "ListComp", 1, (1, 4), (1, 9), "a", b"a")

    replacement = eec._replacement(cand).decode()

    assert replacement.startswith(f'({eec.MARK_FN}("{cand.probe}"), ')
    assert replacement.endswith(f"{cand.probe}())[1]")
    # `[1]` and not `[0]`: the undefined call has to be the one whose
    # value the comprehension takes, or it never runs.
    assert ast.parse(replacement, mode="eval")


def test_the_marker_helper_records_each_probe_once(tmp_path, monkeypatch):
    """It runs inside the mutated module, once per element per suite run,
    so it writes the first time and stays out of the way after."""
    path = tmp_path / "marks"
    monkeypatch.setenv(eec.MARK_ENV, str(path))
    namespace: dict = {}
    exec(eec.MARK_HELPER, namespace)

    mark = namespace[eec.MARK_FN]
    for _ in range(4):
        assert mark("probe_a") is None
    mark("probe_b")

    assert path.read_text().split() == ["probe_a", "probe_b"]


def test_the_marker_helper_is_silent_with_nowhere_to_write(tmp_path, monkeypatch):
    """A mutated file left behind by a crash must not break an ordinary
    run of the suite, and the suite's own control run has no marker."""
    monkeypatch.delenv(eec.MARK_ENV, raising=False)
    namespace: dict = {}
    exec(eec.MARK_HELPER, namespace)

    assert namespace[eec.MARK_FN]("probe_a") is None


def test_an_element_only_legal_in_parentheses_is_still_spliceable(tmp_path):
    """`[(x := v) for v in xs]` hands back the bytes `x := v`.

    The AST's span for an element EXCLUDES the parentheses around it,
    and a bare walrus is not a parseable expression — so an integrity
    check that PARSED the segment called a valid comprehension a
    position the file and the AST disagreed about, and the run died on
    it. Parsing it parenthesised fixed that and made the check looser
    than it looked: `x for x in values` is a SyntaxError alone and
    parses wrapped. The check compares the exact scanned bytes now, so
    this case passes for the reason it should — the bytes are the ones
    the scan saw — and not because they happen to parse.
    """
    path = tmp_path / "x.py"
    path.write_text(
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/w')\n"
        "async def h():\n"
        "    last = 0\n"
        "    return [(last := v) for v in [1, 2]]\n"
    )
    cand = eec.find_candidates([tmp_path])[0]
    assert cand.source == "last := v"

    assert eec.compile_error(cand) is None
    originals, written = eec.mutate([cand])
    try:
        assert f"{cand.probe}()" in path.read_text()
    finally:
        eec.restore(originals, written)


def test_a_span_holding_different_bytes_is_refused(tmp_path):
    """The file can change after the scan — the control run alone is
    four minutes long — and a stale range that now covers ANOTHER valid
    expression was accepted, so the checker mutated unrelated code and
    reported a coverage verdict about it (Codex round 2)."""
    path = tmp_path / "x.py"
    body = (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/x')\n"
        "async def h(values):\n"
        "    return [%s for v in values]\n"
    )
    path.write_text(body % "render(v)")
    cand = eec.find_candidates([tmp_path])[0]
    assert cand.raw == b"render(v)"

    path.write_text(body % "DIFFERENT(v)")
    before = path.read_bytes()

    with pytest.raises(AssertionError, match="changed under the run"):
        eec.mutate([cand])

    assert path.read_bytes() == before


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
def test_the_scan_records_the_bytes_the_file_actually_holds(tmp_path, newline):
    """`raw` is compared against `read_bytes()`, so it must BE those bytes.

    `read_text` performs universal-newline translation. On a checkout
    with CRLF endings — `core.autocrlf=true`, the Windows default, under
    this repository's `* text=auto` — a MULTI-LINE element was recorded
    holding `\n` while the file held `\r\n`, and the splice check called
    that "the file changed under the run": the guard exited 5 with no
    verdict about anything, on a tree where nothing was wrong (Codex
    round 27).

    Both line endings are exercised, because a fix that normalised BOTH
    sides would pass a CRLF-only case while quietly rewriting every line
    ending in a developer's file the moment it spliced.

    A single-line element was never affected — the `\r` sits past the
    element's end column — which is why this case spans four lines.
    """
    path = tmp_path / "x.py"
    body = (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/x')\n"
        "async def h(rows):\n"
        "    return [\n"
        "        {\n"
        '            "id": r.id,\n'
        "        }\n"
        "        for r in rows\n"
        "    ]\n"
    )
    path.write_bytes(body.encode("utf-8").replace(b"\n", newline))
    on_disk = path.read_bytes()
    assert (b"\r\n" in on_disk) == (newline == b"\r\n"), "the fixture lost its line endings"

    cand = eec.find_candidates([tmp_path])[0]
    assert cand.start[0] != cand.end[0], "this case is about a MULTI-line element"

    # The recorded bytes are the bytes at that span ON DISK, whatever
    # the line endings are -- asserted against an independent extraction
    # rather than against another call to the thing under test.
    start_line, start_col = cand.start
    end_line, end_col = cand.end
    lines = on_disk.split(newline)
    expected = newline.join(
        [lines[start_line - 1][start_col:]]
        + lines[start_line:end_line - 1]
        + [lines[end_line - 1][:end_col]]
    )
    assert cand.raw == expected
    assert (b"\r\n" in cand.raw) == (newline == b"\r\n")

    # ...and the splice agrees with it, which is what actually broke.
    originals, written = eec.mutate([cand])
    try:
        assert f"{cand.probe}()" in path.read_bytes().decode("utf-8")
    finally:
        eec.restore(originals, written)
    assert path.read_bytes() == on_disk


def test_the_scan_takes_one_snapshot_of_each_file(tmp_path, monkeypatch):
    """The parse and `raw` must come from ONE read of the file.

    Taking `raw` from a SECOND read is the smaller fix for the CRLF
    defect above, and it quietly undoes round 24: measured, a save
    landing between the two reads records the NEW bytes as `raw`, the
    splice check then agrees with them, and the mutation lands in code
    nobody scanned. This case puts a save exactly there — any call to
    `read_text` during the scan edits the file — and then restores the
    file, so a scan that took one snapshot still matches and a scan that
    took two does not.
    """
    path = tmp_path / "x.py"
    body = (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/x')\n"
        "async def h(values):\n"
        "    return [render(v) for v in values]\n"
    ).encode("utf-8")
    path.write_bytes(body)

    real_read_text = pathlib.Path.read_text

    def saves_over_it(self, *args, **kwargs):
        text = real_read_text(self, *args, **kwargs)
        if self == path:
            self.write_bytes(body.replace(b"render(v)", b"CHANGED(v)"))
        return text

    monkeypatch.setattr(pathlib.Path, "read_text", saves_over_it)

    cand = eec.find_candidates([tmp_path])[0]
    path.write_bytes(body)  # the save is undone; disk is as the scan found it

    assert cand.raw == b"render(v)", "raw came from a later read, not the scan's own"
    originals, written = eec.mutate([cand])
    try:
        assert f"{cand.probe}()" in path.read_bytes().decode("utf-8")
    finally:
        eec.restore(originals, written)


def test_a_batch_that_will_not_compile_falls_back_instead_of_crashing(tmp_path):
    """Screening one candidate at a time does not settle a BATCH.

    Two comprehensions in one handler each bind `x` with `(x := v)` and
    an inner function declares `nonlocal x`. Mutate either alone and the
    other binding still stands — so both pass `compile_error`. Mutate
    both and neither does, and the `compile` inside `mutate` raised an
    exception that escaped `main`: a valid router module crashed the
    gate instead of being measured (Codex round 2). It raises
    `BatchNotCompilable` now, which the batch loop catches and answers
    by measuring the members one at a time.
    """
    path = tmp_path / "x.py"
    path.write_text(
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/x')\n"
        "async def h(values):\n"
        "    a = [(x := v) for v in values]\n"
        "    b = [(x := v * 3) for v in values]\n\n"
        "    def bump():\n"
        "        nonlocal x\n"
        "        x += 1\n\n"
        "    bump()\n"
        "    return a, b, x\n"
    )
    before = path.read_bytes()
    cands = eec.find_candidates([tmp_path])
    assert len(cands) == 2
    # Each on its own is measurable, which is exactly why the per
    # candidate screen says nothing about the pair.
    assert [eec.compile_error(c) for c in cands] == [None, None]
    assert eec.batches(cands) == [cands], "same depth, so one batch"

    with pytest.raises(eec.BatchNotCompilable, match="will not compile"):
        eec.mutate(cands)

    assert path.read_bytes() == before, "a refused batch must restore the file"
    # And the fallback the loop uses actually works.
    for cand in cands:
        originals, written = eec.mutate([cand])
        try:
            assert f"{cand.probe}()" in path.read_text()
        finally:
            eec.restore(originals, written)


def test_a_mutation_that_would_not_compile_is_reported_not_written(tmp_path):
    """`ast.parse` builds no symbol table.

    Here the walrus is the ONLY binding of `last` in the handler, and an
    inner function declares `nonlocal last`. Removing the element
    removes the binding: the module still PARSES and no longer COMPILES
    (`no binding for nonlocal 'last' found`). Written to disk, that
    turns the gate into one that rejects every change to a file holding
    a perfectly valid comprehension.
    """
    path = tmp_path / "x.py"
    path.write_text(
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        "@router.get('/w')\n"
        "async def h():\n"
        "    out = [(last := v) for v in [1, 2]]\n\n"
        "    def bump():\n"
        "        nonlocal last\n"
        "        last += 1\n\n"
        "    bump()\n"
        "    return out, last\n"
    )
    before = path.read_bytes()
    cand = eec.find_candidates([tmp_path])[0]

    why = eec.compile_error(cand)

    assert why is not None and "nonlocal" in why
    assert path.read_bytes() == before, "a screened candidate must not be written"


def test_a_red_run_quotes_what_failed_and_not_the_warnings():
    """The first red control this guard ever reported named the test and
    not the assertion: `--tb=line` puts the reason in the FAILURES
    section, the warnings summary sits after it, and a fixed tail of the
    output showed the warnings."""
    out = (
        "=================================== FAILURES ===========\n"
        "/repo/backend/tests/test_battery.py:5474: AssertionError: the loop was "
        "blocked for 2.31s while probes were in flight\n"
        "=============================== warnings summary =======\n"
        "tests/test_h6.py::test_sign_in\n"
        "  /venv/_pytest/stash.py:108: RuntimeWarning: coroutine was never awaited\n"
        "-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n"
        "=========================== short test summary info ====\n"
        "FAILED tests/test_battery.py::test_the_battery_does_not_block_the_loop\n"
        "1 failed, 1302 passed in 263.33s\n"
    )

    shown = eec._failures(out)

    assert "blocked for 2.31s" in shown
    assert "FAILED tests/test_battery.py::test_the_battery_does_not_block_the_loop" in shown
    assert "RuntimeWarning" not in shown
    assert "how-to/capture-warnings" not in shown


def test_a_run_with_nothing_to_quote_falls_back_to_the_tail():
    """A suite that collected no tests has no failure line at all, and
    an empty report would be worse than the last few lines of output."""
    out = "noise\n" * 3 + "no tests ran in 0.01s\n"

    assert "no tests ran" in eec._failures(out)


def test_the_control_runs_summary_line_is_reported(tmp_path):
    """A green suite and a suite that did not run are the same exit code;
    the counts are what tells them apart, so a finding carries them."""
    out = "some noise\n=========== 1274 passed, 14 warnings in 257.96s (0:04:17) ====\n"

    assert eec._summary_line(out).startswith("1274 passed")
    assert eec._summary_line("no summary here") == "(pytest printed no summary line)"


def test_a_nested_route_handler_yields_one_candidate_per_span(tmp_path):
    """One route defined inside another is scanned twice, and was.

    `ast.walk` reaches the inner comprehension from the OUTER handler's
    body, and the inner handler's own scan reaches it again, so two
    candidates carried the same byte range. Neither contains the other,
    so they landed in one batch, and the second splice read the bytes
    the first one had already written: `_mutated_bytes` raised
    `AssertionError` and the job died with no verdict at all (Codex
    round 18).

    It died saying *the file changed under the run*, which is the one
    thing that had not happened — so the crash sent its reader looking
    for a concurrent edit. That is why this asserts the batch mutates
    cleanly and not merely that the count is one.

    The innermost attribution wins, because the inner handler is the one
    that runs the expression. Descending is KEPT: a comprehension in a
    plain nested helper is still run by the route that calls it, and
    refusing to enter nested definitions would stop measuring it — a
    check lost to fix a crash.
    """
    package = tmp_path / "routers"
    package.mkdir()
    (package / "nest.py").write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "_ROWS = [1, 2]\n"
        "\n"
        "\n"
        '@router.get("/outer")\n'
        "def outer():\n"
        '    @router.get("/inner")\n'
        "    def inner():\n"
        "        return [str(v) for v in _ROWS]\n"
        "\n"
        "    return {}\n",
        encoding="utf-8",
    )

    found = eec.find_candidates([package])

    assert len(found) == 1
    assert found[0].handler == "inner"
    assert [c.index for c in found] == [0]

    # The crash itself, not just its cause: one batch, spliced once.
    data = found[0].path.read_bytes()
    for group in eec.batches(found):
        eec._mutated_bytes(data, group)


def test_candidate_indices_are_contiguous_after_deduplication(tmp_path):
    """`batches` keys containment depth BY INDEX.

    Handing indices out at append time and then dropping a duplicate
    leaves a gap, and two candidates sharing an index would silently
    mis-group — a batch is only sound because its members cannot
    contain one another. The numbering is therefore done after the
    duplicate is removed, and this pins it.
    """
    package = tmp_path / "routers"
    package.mkdir()
    (package / "many.py").write_text(
        "from fastapi import APIRouter\n"
        "\n"
        "router = APIRouter()\n"
        "_ROWS = [1, 2]\n"
        "\n"
        "\n"
        '@router.get("/a")\n'
        "def a():\n"
        "    return [str(v) for v in _ROWS]\n"
        "\n"
        "\n"
        '@router.get("/b")\n'
        "def b():\n"
        '    @router.get("/c")\n'
        "    def c():\n"
        "        return [repr(v) for v in _ROWS]\n"
        "\n"
        "    return [hex(v) for v in _ROWS]\n",
        encoding="utf-8",
    )

    found = eec.find_candidates([package])

    assert [c.index for c in found] == list(range(len(found)))
    assert len(found) == 3
    assert sorted(c.handler for c in found) == ["a", "b", "c"]


def test_an_untracked_candidate_file_is_not_a_clean_tree(tmp_path):
    """`git diff HEAD -- <path>` says nothing about a file HEAD lacks.

    It exits 0 with no output for an untracked path, so a newly written
    router read as clean — and so did the same file still holding a
    mutation an interrupted run had left in it, which is precisely the
    state this preflight exists to refuse. The next run would splice on
    top of it and report a verdict about code nobody wrote (Codex round
    19).

    Third defect in this one function: a failed `git diff` read as
    clean (round 1), and now "no output" meaning two different things.
    Both are the same mistake — reading absence of evidence as
    evidence — which is why the tracked check is a separate question
    asked first rather than another way of reading the diff.
    """
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    committed = repo / "kept.py"
    committed.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "kept.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "i"],
        cwd=repo, check=True,
    )
    untracked = repo / "new_router.py"
    untracked.write_text("# never committed\n", encoding="utf-8")

    previous = pathlib.Path.cwd()
    try:
        import os

        os.chdir(repo)
        clean, detail = eec._tree_is_clean({untracked})
        assert clean is False
        # The REASON, not merely a refusal: "HEAD" alone would also be
        # satisfied by round 1's message, and this has to be the one
        # about an absent version rather than an unanswerable git.
        assert "not tracked" in detail
        assert "git could not report the state" not in detail

        # The three states must stay distinguishable, or a rule that
        # simply refused everything would pass the assertion above.
        assert eec._tree_is_clean({committed})[0] is True
        committed.write_text("x = 2\n", encoding="utf-8")
        assert eec._tree_is_clean({committed})[0] is False
    finally:
        os.chdir(previous)


def _handler_module(decorator: str) -> str:
    """A router module whose ONE handler carries `decorator`."""
    return (
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "_ROWS = [1, 2]\n"
        f"{decorator}\n"
        "async def handler(rows=_ROWS) -> dict:\n"
        "    return {'rows': [_to_row(v) for v in rows]}\n"
        "def _to_row(v):\n"
        "    return {'v': v}\n"
    )


def _registering_decorator_names() -> set[str]:
    """The names that REGISTER a route on FastAPI's `APIRouter`, measured.

    Each public attribute is used as `@router.<name>("/p")` against a
    fresh router and kept if `router.routes` grew. Derived rather than
    retyped, for the reason this PR has now recorded four times: a
    hand-kept copy of something measurable goes stale, and the one in
    `ROUTE_DECORATOR_ATTRS` went stale in a way that let element
    expressions bypass the guard entirely (Codex round 21).
    """
    import warnings

    from fastapi import APIRouter

    async def endpoint():
        return {}

    found = set()
    for name in dir(APIRouter):
        if name.startswith("_") or not callable(getattr(APIRouter, name, None)):
            continue
        router = APIRouter()
        before = len(router.routes)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                decorator = getattr(router, name)("/p")
                if not callable(decorator):
                    continue
                decorator(endpoint)
        except Exception:
            # Needs other arguments, or is not a decorator at all.
            continue
        if len(router.routes) > before:
            found.add(name)
    return found


def test_the_guards_route_decorator_set_covers_what_fastapi_registers():
    """The guard must know every spelling that makes a route handler.

    `ROUTE_DECORATOR_ATTRS` held five names and FastAPI registers
    twelve, so a handler written with `@router.api_route(...)`,
    `@router.options`, `@router.head`, `@router.trace`, `@router.route`,
    `@router.websocket` or `@router.websocket_route` was not a route
    handler as far as the guard was concerned -- its element
    expressions bypassed the gate while the commit adding it still
    triggered the job. Codex round 21 named three of the seven;
    measuring the rest is what found the other four.

    Coverage, not equality: a name the guard knows and FastAPI has
    dropped can only over-include, which costs a spurious candidate,
    while a name FastAPI registers and the guard does not know is the
    hole this test exists for. The non-empty assertion is not
    decoration -- a derivation that silently returned nothing would
    make the coverage assertion true by having nothing to check, which
    is the shape of defect this whole batch is about.
    """
    registering = _registering_decorator_names()

    assert len(registering) >= 5, (
        f"the derivation found only {sorted(registering)}; it cannot vouch "
        f"for the guard's set if it cannot see FastAPI's own verbs"
    )
    assert {"get", "post", "websocket", "api_route"} <= registering, (
        f"the derivation is not measuring what it claims: {sorted(registering)}"
    )
    missing = registering - eec.ROUTE_DECORATOR_ATTRS
    assert not missing, (
        f"FastAPI registers routes through {sorted(missing)} and the guard "
        f"does not know them, so a handler declared that way has its element "
        f"expressions skipped while the job still runs. Add them to "
        f"ROUTE_DECORATOR_ATTRS."
    )


@pytest.mark.parametrize(
    "decorator",
    [
        '@router.get("/p")',
        '@router.post("/p")',
        '@router.put("/p")',
        '@router.patch("/p")',
        '@router.delete("/p")',
        '@router.head("/p")',
        '@router.options("/p")',
        '@router.trace("/p")',
        '@router.route("/p")',
        '@router.api_route("/p", methods=["GET"])',
        '@router.websocket("/p")',
        '@router.websocket_route("/p")',
    ],
)
def test_a_handler_is_found_whatever_spelling_registered_it(tmp_path, decorator):
    """Behavioural, beside the set-covers-it check above.

    That one compares two collections of names; this one drives
    `find_candidates` and asserts a handler declared each way yields
    its element expression. Both are here because the first would stay
    green if `_route_verb` stopped consulting the set at all.
    """
    root = tmp_path / "routers"
    root.mkdir()
    (root / "r.py").write_text(_handler_module(decorator))

    found = eec.find_candidates([root])

    assert [c.handler for c in found] == ["handler"], (
        f"{decorator} produced {[(c.handler, c.kind) for c in found]}"
    )


def _registration_surface() -> dict[str, int]:
    """`{method: positional index of its endpoint}`, measured.

    FastAPI registers a route imperatively through any `APIRouter`
    method taking an `endpoint`; the index is read from the signature
    rather than assumed, because "it is always second" is exactly the
    kind of claim this PR has been wrong about four times.
    """
    import inspect

    from fastapi import APIRouter

    surface = {}
    for name in dir(APIRouter):
        if name.startswith("_"):
            continue
        fn = getattr(APIRouter, name, None)
        if not callable(fn):
            continue
        try:
            parameters = list(inspect.signature(fn).parameters)
        except (ValueError, TypeError):
            continue
        if "endpoint" in parameters:
            surface[name] = parameters[1:].index("endpoint")
    return surface


def test_the_guard_knows_every_imperative_registration():
    """A route registered without a decorator is still a route.

    `router.add_api_route("/x", handler, methods=["GET"])` produces the
    same route as `@router.get("/x")`, and the scan looked only at
    decorators -- so every element expression in such a handler bypassed
    the gate while the commit adding it still triggered the job. Round
    21 closed the decorator half of this hole and RECORDED this half as
    a limitation instead of closing it; Codex round 22 declined that,
    correctly.

    Derived and asserted the same way as the decorator set, and for the
    same reason: a hand-kept list of these went stale within one round
    of being written down. The non-empty assertion is load-bearing --
    an empty derivation would make the coverage check below true by
    having nothing to check.
    """
    surface = _registration_surface()

    assert len(surface) >= 2, (
        f"the derivation found only {sorted(surface)}; it cannot vouch for "
        f"the guard's set if it cannot see FastAPI's own registration methods"
    )
    assert "add_api_route" in surface, (
        f"the derivation is not measuring what it claims: {sorted(surface)}"
    )
    missing = set(surface) - eec.ROUTE_REGISTRATION_ATTRS
    assert not missing, (
        f"FastAPI registers routes through {sorted(missing)} and the guard "
        f"does not know them, so a handler registered that way has its "
        f"element expressions skipped while the job still runs. Add them to "
        f"ROUTE_REGISTRATION_ATTRS."
    )
    wrong = {n: i for n, i in surface.items() if i != eec.ROUTE_ENDPOINT_POSITION}
    assert not wrong, (
        f"the endpoint is not where the guard looks for it in {wrong}; "
        f"ROUTE_ENDPOINT_POSITION is {eec.ROUTE_ENDPOINT_POSITION}"
    )


@pytest.mark.parametrize(
    "call, found",
    [
        ("router.add_api_route('/x', handler, methods=['GET'])", True),
        ("router.add_api_route('/x', endpoint=handler)", True),
        ("router.add_route('/x', handler, methods=['GET'])", True),
        ("router.add_websocket_route('/x', handler)", True),
        ("router.add_api_websocket_route('/x', handler)", True),
        # A lifecycle hook is not a route.
        ("router.add_event_handler('startup', handler)", False),
        # Not a plain name: this scan cannot follow it to a definition,
        # and guessing one would be an invention.
        ("router.add_api_route('/x', make_handler())", False),
    ],
)
def test_an_imperatively_registered_handler_is_a_candidate(tmp_path, call, found):
    """Behavioural, beside the surface check above, which compares only
    names and would stay green if `find_candidates` stopped consulting
    the set at all."""
    root = tmp_path / "routers"
    root.mkdir()
    (root / "r.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "_ROWS = [1, 2]\n"
        "async def handler(rows=_ROWS) -> dict:\n"
        "    return {'rows': [_to_row(v) for v in rows]}\n"
        "def _to_row(v):\n"
        "    return {'v': v}\n"
        "def make_handler():\n"
        "    return handler\n"
        f"{call}\n"
    )

    candidates = eec.find_candidates([root])

    assert bool(candidates) is found, (
        f"{call} -> {[(c.handler, c.kind) for c in candidates]}"
    )


def test_restore_refuses_to_overwrite_an_edit_made_during_the_run(tmp_path):
    r"""A file saved while the suite runs is the developer's, not ours.

    The control run takes four minutes and the mutated tree IS the
    working checkout, so an edit landing inside that window is ordinary.
    `restore` used to write the pre-run bytes back unconditionally,
    which destroyed it silently: the preflight and the `raw` comparison
    both look BEFORE the mutation and nothing looked after it (Codex
    round 26).

    A file still holding exactly what this run wrote is ours to put
    back. One that does not is left alone and reported — losing a
    mutation the developer can see and undo beats losing work they
    cannot get back.
    """
    target = tmp_path / "router.py"
    target.write_bytes(b"ORIGINAL\n")
    originals = {target: target.read_bytes()}
    target.write_bytes(b"MUTATED\n")
    written = {target: b"MUTATED\n"}

    # the ordinary path: untouched since the mutation, so restored
    eec.restore(originals, written)
    assert target.read_bytes() == b"ORIGINAL\n"

    # ...and the one that matters
    target.write_bytes(b"MUTATED\n")
    target.write_bytes(b"WORK SAVED WHILE PYTEST RAN\n")
    with pytest.raises(eec.RestoreConflict, match="edited while the suite"):
        eec.restore(originals, written)
    assert target.read_bytes() == b"WORK SAVED WHILE PYTEST RAN\n", (
        "restore overwrote an edit it did not make"
    )


def test_a_scan_root_that_is_not_there_is_refused_not_reported_clean(tmp_path):
    r"""`rglob` is silent about a missing directory; the gate must not be.

    Measured: `Path.rglob` on a directory that does not exist yields
    nothing, exactly as a real-but-empty one does. So renaming
    `backend/app/routers` made the checker print "no candidates" and
    exit 0 — reporting success having looked at nothing, which is the
    failure it exists to catch one level up. The deletion triggers the
    workflow once; every change under the new location then goes
    unguarded while the job stays green (Codex round 26).

    An empty REAL root still exits 0: a repository whose handlers build
    no comprehension is a legitimate clean answer, and refusing it
    would make the gate unsatisfiable.
    """
    import subprocess

    def run(*roots):
        return subprocess.run(
            [sys.executable, str(_CHECKER), "--roots", *roots],
            capture_output=True, text=True,
            cwd=str(_CHECKER.parents[1]),
        )

    gone = run(str(tmp_path / "not-a-directory"))
    assert gone.returncode == 5, gone.stdout + gone.stderr
    assert "REFUSING" in gone.stderr and "not directories" in gone.stderr

    empty = tmp_path / "real-but-empty"
    empty.mkdir()
    ok = run(str(empty))
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "no candidates" in ok.stdout
