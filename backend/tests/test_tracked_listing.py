"""Every tracked-file listing reads each path ONCE, and survives the
paths git prints cooked.

`git ls-files` has two properties that a `.split()` and a bare list get
wrong, and both were found for real rather than imagined:

1. **it lists a path once per index STAGE.** During an unresolved merge
   a conflicted file appears three times. `scripts/check_tables.py` was
   fixed for this (§12 211(j)) and the two other enumerating callers
   were not — the register's most repeated finding, a rule applied
   where the report pointed rather than everywhere it is true. Measured
   mid-merge on two separate merges: `check_links.py` reported 63 files
   and then 61, where the tree has 59.
2. **its default output is COOKED.** A path holding a space survives
   `.split()` as two paths that name no file; one holding a non-ASCII
   character is printed quoted and octal-escaped. `-z` turns both off.

The stage cases drive a REAL unresolved merge rather than a stubbed
`subprocess`, because a stub is a second copy of the wire format and the
thing under test is what git actually prints.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # @dataclass resolves its module by name
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(
        ["git", *args], cwd=repo, check=check, capture_output=True, text=True,
    )


def _repo_mid_merge(tmp_path: Path, filename: str) -> Path:
    """A repository stopped in an unresolved merge, so `filename` really
    does carry three index stages. Nothing here is simulated."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    target = repo / filename
    target.write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    _git(repo, "checkout", "-qb", "other")
    target.write_text("theirs\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "theirs")
    _git(repo, "checkout", "-q", "-")
    target.write_text("ours\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "ours")
    _git(repo, "merge", "other", check=False)          # leaves the conflict
    stages = _git(repo, "ls-files", filename).stdout.split("\n")
    assert len([s for s in stages if s]) == 3, (
        f"this fixture is meant to produce three index stages, got {stages}. "
        f"Without them the cases below prove nothing."
    )
    return repo


@pytest.mark.parametrize(
    "script, attr, filename",
    [
        ("check_links", "tracked_markdown", "doc.md"),
        ("check_tables", "tracked_markdown", "doc.md"),
    ],
)
def test_a_conflicted_file_is_listed_once(tmp_path, script, attr, filename):
    """THE DEFECT: three stages read as three files."""
    module = _load(script)
    repo = _repo_mid_merge(tmp_path, filename)
    module.ROOT = repo

    found = getattr(module, attr)()
    assert [p.name for p in found] == [filename], [str(p) for p in found]


@pytest.mark.parametrize("script, attr", [("check_links", "tracked_markdown"),
                                          ("check_tables", "tracked_markdown")])
def test_a_path_with_a_space_is_one_path_that_exists(tmp_path, script, attr):
    """`.split()` is not a parser: `release notes.md` came back as
    `release` and `notes.md`, two paths naming no file."""
    module = _load(script)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "release notes.md").write_text("# T\n", encoding="utf-8")
    (repo / "plain.md").write_text("# T\n", encoding="utf-8")
    _git(repo, "add", "-A")
    module.ROOT = repo

    found = getattr(module, attr)()
    assert {p.name for p in found} == {"release notes.md", "plain.md"}
    assert all(p.exists() for p in found), [str(p) for p in found]


def test_every_enumerating_ls_files_call_asks_for_z():
    """The rule, derived from the tree rather than remembered.

    A call that ENUMERATES must pass `-z`, or it gets git's cooked
    output. A call that merely ASKS whether a path is tracked
    (`--error-unmatch`) enumerates nothing and is not covered — that
    distinction is the reason this is a rule about enumeration and not
    about the string `ls-files`, and a census that ignored it would
    accuse `scripts/element_expression_coverage.py`, which is correct.
    """
    problems = []
    scripts = sorted(SCRIPTS.glob("*.py"))
    assert scripts, f"no scripts under {SCRIPTS} — this guard checked nothing"
    for path in scripts:
        text = path.read_text(encoding="utf-8")
        for line_no, line in enumerate(text.splitlines(), 1):
            if '"ls-files"' not in line:
                continue
            if "--error-unmatch" in line:
                continue                     # a membership query, not a listing
            if '"-z"' not in line:
                problems.append(
                    f"{path.name}:{line_no}: `git ls-files` enumerates without "
                    f"`-z`, so a tracked path holding a space or a non-ASCII "
                    f"character comes back cooked and names no file"
                )
    assert not problems, "\n".join(problems)
