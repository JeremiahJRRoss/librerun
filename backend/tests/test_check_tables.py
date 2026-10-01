"""`scripts/check_tables.py` — negative-tested by injection.

GFM drops table cells past the header count, silently. The gate exists
because seventeen rows of the gap register did exactly that and thirteen
of them lost the Owner column, which is where that file records whether
a gap is closed (issue #82).

A gate that reports success by not looking is worse than no gate, so
every case here either **plants the defect and requires a finding**, or
plants a row that is CORRECT and requires silence. Both directions
matter: a checker that flags `\\|` would report two already-correct rows
as broken, and a checker that assumes a trailing `|` would drop the last
cell of every row that omits one — a parser losing content while
measuring a file that loses content. The census that found #82 made
both mistakes before it made neither.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_tables.py"


def _module():
    """The script as a module.

    Registered in `sys.modules` BEFORE it is executed, because
    `@dataclass` resolves its own module by name while the class body
    runs — without this the import raises `AttributeError: 'NoneType'
    object has no attribute '__dict__'`, which says nothing about the
    cause.
    """
    spec = importlib.util.spec_from_file_location("check_tables", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_tables"] = module
    spec.loader.exec_module(module)
    return module


def _run(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *[str(p) for p in paths]],
        capture_output=True, text=True, cwd=ROOT,
    )


HEADER = "| ID | Gap | Sev | Evidence | Target state | Owner |\n"
RULE = "|----|-----|-----|----------|--------------|-------|\n"
GOOD = "| A1 | a gap | S1 | some evidence | a target | S4 — **closed** |\n"


def _doc(tmp_path: Path, *rows: str, name: str = "t.md") -> Path:
    path = tmp_path / name
    path.write_text("# T\n\n" + HEADER + RULE + "".join(rows) + "\n", encoding="utf-8")
    return path


def test_the_real_tree_is_clean_and_says_how_much_it_looked_at():
    """The rule on the tree, with the examined count beside the verdict —
    otherwise "no findings" and "found no tables" read the same."""
    result = _run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean: all " in result.stdout
    counted = int(result.stdout.split("clean: all ")[1].split(" row")[0])
    assert counted > 1000, f"only {counted} rows examined; the tree has more"


def test_each_tracked_file_is_read_once(monkeypatch):
    """`git ls-files` lists a path once per index STAGE.

    During an unresolved merge a conflicted file appears three times, so
    the gate counted its tables three times — 59 files read as 61 and
    1404 rows as 1648. Found while resolving a conflict in the very
    register this gate exists for.
    """
    module = _module()
    calls = {}

    class _Result:
        # NUL-separated, because that is what `-z` prints. A stub is a
        # second copy of the wire format, so the argv is asserted too:
        # when this test fed newlines and the script moved to `-z`, the
        # stub still "passed" its own shape and the real git output was
        # the thing nobody was testing.
        stdout = "a.md\0b.md\0a.md\0a.md\0"

    def fake_run(*args, **kwargs):
        calls["argv"] = list(args[0])
        return _Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    files = module.tracked_markdown()
    assert "-z" in calls["argv"], calls["argv"]
    assert [p.name for p in files] == ["a.md", "b.md"], files


def test_an_extra_cell_is_a_finding(tmp_path):
    """THE DEFECT, injected: the shape that loses the Owner column."""
    doc = _doc(tmp_path, GOOD, "| A2 | a gap | S1 | evidence | target |  | S4 — closed |\n")
    result = _run(doc)
    assert result.returncode == 1
    assert "'A2' has 7 cells" in result.stdout
    assert "not on the page" in result.stdout
    assert "'A1'" not in result.stdout, "the well-formed row must not be accused"


def test_a_missing_cell_is_a_finding_too(tmp_path):
    """The other direction. A short row renders with blanks rather than
    dropping text, but it still means the row does not say what its
    header claims it says."""
    doc = _doc(tmp_path, GOOD, "| A2 | a gap | S1 | evidence |\n")
    result = _run(doc)
    assert result.returncode == 1
    assert "'A2' has 4 cells, fewer than" in result.stdout


def test_an_escaped_pipe_is_content_and_not_a_separator(tmp_path):
    """PARSER DETAIL ONE, asserted as silence.

    `C8` and `E2` of the real register escape their content pipes and are
    correct; a checker splitting on every `|` reports both as broken.
    """
    doc = _doc(tmp_path, "| A2 | the set is `;\\|&<>` | S1 | ev | target | S4 |\n")
    result = _run(doc)
    assert result.returncode == 0, result.stdout


def test_a_row_with_no_trailing_pipe_keeps_its_last_cell(tmp_path):
    """PARSER DETAIL TWO, asserted as silence.

    A trailing `|` is optional in GFM. A checker that assumes one
    discards the last cell of every row without it — and then reports a
    correct six-cell row as a five-cell one.
    """
    doc = _doc(tmp_path, "| A2 | a gap | S1 | evidence | target | S4 — **closed**\n")
    result = _run(doc)
    assert result.returncode == 0, result.stdout
    module = _module()
    assert len(module.split_cells("| a | b | c")) == 3
    assert len(module.split_cells("| a | b | c |")) == 3


def test_a_table_inside_a_fence_is_an_example_not_a_defect(tmp_path):
    """A code block showing a broken row — this gate's own docstring does
    — is documentation, and flagging it makes the rule unwritable."""
    path = tmp_path / "f.md"
    path.write_text(
        "# T\n\n```\n" + HEADER + RULE + "| A2 | x | y | z | w | v | EXTRA |\n```\n"
        "\n" + HEADER + RULE + GOOD + "\n",
        encoding="utf-8",
    )
    result = _run(path)
    assert result.returncode == 0, result.stdout


def test_a_run_that_finds_no_table_fails_rather_than_reporting_clean(tmp_path):
    """THE VACUITY GUARD, injected by giving it a file with no table.

    Without it, a bad path, an empty `git ls-files`, or a fence-tracking
    bug that swallowed a whole file all print "clean" — the exact shape
    this repository refuses: a gate reporting success by not looking.
    """
    path = tmp_path / "empty.md"
    path.write_text("# No tables here\n\nJust prose.\n", encoding="utf-8")
    result = _run(path)
    assert result.returncode == 1
    assert "looked at nothing" in result.stdout


def test_two_tables_in_one_file_are_held_to_their_own_widths(tmp_path):
    """The gap register has fifteen tables of four different widths, so
    the width is the TABLE's, never the file's."""
    path = tmp_path / "two.md"
    path.write_text(
        "# T\n\n" + HEADER + RULE + GOOD + "\n"
        "| A | B |\n|---|---|\n| one | two |\n| three | four | FIVE |\n",
        encoding="utf-8",
    )
    result = _run(path)
    assert result.returncode == 1
    assert "'three' has 3 cells" in result.stdout
    assert "'A1'" not in result.stdout


def test_a_row_outside_every_table_is_a_finding(tmp_path):
    """THE OTHER HALF OF THE SAME BUG, and this gate committed it.

    Five rows of the register were APPENDED to the end of the file, after
    its closing prose, with no header above them — so GFM rendered them
    as literal text, pipes and all. The row-count check said "clean",
    because it counts rows IN tables and those were in none: a gate
    reporting success by not looking, inside the fix for a gate that
    reported success by not looking. The count not moving when a row was
    added is what gave it away.
    """
    path = tmp_path / "orphan.md"
    path.write_text(
        "# T\n\n" + HEADER + RULE + GOOD + "\n"
        "Some closing prose.\n\n"
        "| A9 | a gap | S1 | evidence | target | S4 — **closed** |\n",
        encoding="utf-8",
    )
    result = _run(path)
    assert result.returncode == 1
    assert "row 'A9' is in no table" in result.stdout
    assert "renders the whole line as text" in result.stdout


def test_prose_that_merely_begins_with_a_pipe_is_not_a_row(tmp_path):
    """…and the rule is narrowed to earn that, asserted as silence.

    A retired planning document quoted a shell pipeline whose line
    wraps, so the continuation begins
    `| awk '{print $1}')`. A rule that accused it would be demanding churn
    on correct prose, which is how a gate gets switched off. A row copied
    out of a table carries both delimiters and three or more cells; a
    wrapped sentence does not.
    """
    path = tmp_path / "prose.md"
    path.write_text(
        "# T\n\n" + HEADER + RULE + GOOD + "\n"
        "Measured with `head=$(python -m alembic heads\n"
        "| awk '{print $1}')` in `database-parity.yml` and two others.\n",
        encoding="utf-8",
    )
    result = _run(path)
    assert result.returncode == 0, result.stdout


def test_a_path_that_names_no_file_is_a_finding_not_a_silent_skip(tmp_path):
    """A gate that did not read a file has not found it clean.

    `main` skipped a non-existent path with a bare `continue` and still
    counted it in the verdict's total, so the run below printed
    `clean: all 1 row(s) in 1 table(s) across 2 Markdown file(s)` and
    exited 0 — a gate naming a file it never opened. That is the shape
    this whole script exists to forbid, one level in, and it is the
    same shape as the orphan rows: the number was the thing that gave
    it away, so the number has to be the number of files READ.
    """
    good = _doc(tmp_path, GOOD)
    result = _run(good, tmp_path / "absent.md")
    assert result.returncode == 1, result.stdout
    assert "no such file" in result.stdout
    assert "1 Markdown file(s) read" in result.stdout
    # …and it must not claim the file it could not open.
    assert "2 Markdown file(s)" not in result.stdout


def test_the_verdict_counts_the_files_it_read(tmp_path):
    """The positive control for the case above: without it, a verdict
    that said `1` for every run would pass that test while lying here."""
    first = _doc(tmp_path, GOOD, name="a.md")
    second = _doc(tmp_path, GOOD, name="b.md")
    result = _run(first, second)
    assert result.returncode == 0, result.stdout
    assert "across 2 Markdown file(s)" in result.stdout


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_a_tracked_path_with_a_space_is_one_path_that_exists(tmp_path):
    """`git ls-files` output is cooked, and `.split()` is not a parser.

    A tracked `release notes.md` came back from `.split()` as
    `release` and `notes.md` — two paths that name no file, which
    `main` then dropped without a word while counting both. Driven
    against a real repository rather than a stubbed `subprocess`,
    because the defect IS in what git prints.
    """
    module = _module()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "release notes.md").write_text("# T\n", encoding="utf-8")
    (repo / "plain.md").write_text("# T\n", encoding="utf-8")
    _git(repo, "add", "-A")
    module.ROOT = repo

    found = module.tracked_markdown()
    assert len(found) == 2, [str(p) for p in found]
    assert all(p.exists() for p in found), [str(p) for p in found]
    assert {p.name for p in found} == {"release notes.md", "plain.md"}
