#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Every row of every Markdown table carries its own header's cell count.

    python3 scripts/check_tables.py             # from the repository root
    python3 scripts/check_tables.py --list      # print the files it reads
    python3 scripts/check_tables.py --census    # every table, with its shape

**GFM silently drops cells past the header count.** A row with seven
cells under a six-column header renders with six and the seventh is
gone — no warning, no marker, nothing in the page to say a column of
text was discarded. That is not a formatting nit: in
``docs/Community_Release_Gap_Analysis.md`` seventeen rows carried more
cells than their header, and for thirteen of them what the render
dropped was the **Owner** column, which is where that register records
whether a gap is closed. ``H14``'s own row rendered with the status
narrative in the Owner column and the actual owner, ``S5``, nowhere on
the page (issue #82).

Nothing in this tree parsed those files, so a row that rendered wrong
had never had anything to notice it.

**Two details in the parser are the whole difficulty**, and the census
that first found this got both wrong before it got them right:

1. **A trailing ``|`` is OPTIONAL in GFM.** Assuming every row ends with
   one discards the last cell of every row that does not — a parser
   quietly dropping content while measuring a file that quietly drops
   content.
2. **``\\|`` is CONTENT, not a separator.** Splitting on every ``|``
   counts an escaped pipe as a column break and reports correct rows as
   broken; two rows in that register were already escaping theirs, and
   the first version of the census accused both.

Scope is every tracked ``*.md``, and that is a measurement rather than a
preference: across 59 files, 205 tables and 1399 body rows, **exactly
one file was ragged** — so a repository-wide rule costs no churn and
covers the next file to grow a table, which a rule scoped to the one
known offender would not.

**A row that belongs to no table is the other half of the same bug**,
and this gate learned it by committing it. Appending rows to the END of
this register instead of into its table left five of them after the
closing prose, with no header above — so GFM rendered them as literal
text, pipes and all, and the row-count check said "clean" because it
counts rows IN tables and those were in none. The count not moving when
a row was added is what gave it away. A pipe-line that is never
followed by a separator row is reported.

Exit code 0 when every row matches its header and every row-looking line
sits in a table, 1 when any does not.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: A separator row: only dashes, colons and spaces, and never empty.
_SEPARATOR = re.compile(r"^[\s:-]+$")


def split_cells(line: str) -> list[str]:
    """The cells of one table row, GFM's way.

    The leading and trailing pipes are optional delimiters; everything
    between unescaped pipes is a cell. `\\|` stays where it is, because
    it is a pipe the author wrote as content.
    """
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return re.split(r"(?<!\\)\|", s)


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        c.strip() and _SEPARATOR.fullmatch(c.strip()) for c in cells
    )


@dataclass
class Table:
    path: Path
    header_line: int
    width: int
    rows: list[tuple[int, int, str]]  # (line number, cell count, row id)


@dataclass
class Scan:
    tables: list[Table]
    orphans: list[tuple[int, str]]  # (line number, its first cell)


def tables_in(path: Path) -> Scan:
    """Every table in one file, and every row-looking line outside one.

    A fenced block is not table content — a code sample showing a broken
    row is an example, not a defect.

    A pipe-line is taken as a header only once the NEXT line turns out to
    be a separator. When it does not, that line was never a header: GFM
    renders it as text, and it is reported as an orphan rather than
    quietly becoming the header of a table that does not exist.
    """
    found: list[Table] = []
    orphans: list[tuple[int, str]] = []
    header: tuple[list[str], int, str] | None = None
    current: Table | None = None
    fenced = False

    def close_header() -> None:
        """A pending 'header' that no separator followed was not a header.

        Reported only when the line is unmistakably table-shaped: a
        leading AND a trailing pipe, and at least three cells. Prose
        wraps — `§12` of the blueprint has a shell pipeline whose
        continuation line begins with `| awk …` — and a rule that
        accused it would be demanding churn on correct text, which is
        how a gate gets switched off. A row copied out of a table has
        both delimiters; a wrapped sentence does not.
        """
        nonlocal header
        if header is not None:
            cells, number, raw = header
            stripped = raw.strip()
            table_shaped = (
                stripped.startswith("|")
                and stripped.endswith("|")
                and not stripped.endswith("\\|")
                and len(cells) >= 3
            )
            if table_shaped:
                orphans.append((number, split_cells_first(cells)))
            header = None

    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            close_header()
            current = None
            continue
        if fenced:
            continue
        if not line.strip().startswith("|"):
            close_header()
            current = None
            continue
        cells = split_cells(line)
        if current is not None:
            current.rows.append((number, len(cells), cells[0].strip()))
            continue
        if header is None:
            header = (cells, number, line)
            continue
        if _is_separator(cells):
            current = Table(path, header[1], len(header[0]), [])
            found.append(current)
            header = None
            continue
        # Two pipe-lines and no separator: the first was not a header.
        close_header()
        header = (cells, number, line)
    close_header()
    return Scan(found, orphans)


def split_cells_first(cells: list[str]) -> str:
    return cells[0].strip() if cells else ""


def _display(path: Path) -> str:
    """The path as a reader knows it. A file given on the command line
    may sit outside the repository — that is how the negative tests drive
    this — and `relative_to` RAISES there rather than returning the
    absolute path, which would turn every such run into a traceback."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def tracked_markdown() -> list[Path]:
    """Every tracked Markdown file, each of them ONCE.

    `git ls-files` lists a path once per index STAGE, so during an
    unresolved merge a conflicted file appears three times — and this
    gate then counts its tables three times and would report the same
    finding three times. Found while resolving a conflict in the very
    register this gate exists for: 59 files read as 61 and 1404 rows as
    1648. Order is preserved so the output stays stable.

    `-z`, and split on NUL, because the default output is *cooked*: a
    path holding a space survives `.split()` as two paths that do not
    exist, and one holding a non-ASCII character is printed quoted and
    octal-escaped (`"docs/caf\303\251.md"`), which names no file either.
    Both then failed the existence test in `main` and were dropped
    without a word — a gate not looking at a file while counting it as
    read, which is the shape this whole script exists to forbid. There
    is no such path in this tree today; that is luck, and the next
    contributor's is not this gate's to spend.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "*.md"], cwd=ROOT, capture_output=True, text=True,
        check=True,
    ).stdout.split("\0")
    return [ROOT / p for p in dict.fromkeys(filter(None, out))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="files to check (default: every tracked *.md)")
    parser.add_argument("--list", action="store_true", help="print the files it reads")
    parser.add_argument("--census", action="store_true", help="print every table's shape")
    args = parser.parse_args(argv)

    files = [Path(p).resolve() for p in args.paths] if args.paths else tracked_markdown()
    if args.list:
        for path in files:
            print(_display(path))
        return 0

    problems: list[str] = []
    tables = rows = read = 0
    for path in files:
        # NOT a silent `continue`. A path that names no file was read by
        # nothing, and skipping it quietly while still counting it in the
        # verdict's total made this gate claim a file it never opened:
        # measured, `check_tables.py ok.md absent.md` printed "clean: all
        # 1 row(s) in 1 table(s) across 2 Markdown file(s)" and exited 0.
        # The count below is now what was READ, and the absence is a
        # finding of its own.
        if not path.exists():
            problems.append(
                f"{_display(path)}: no such file — this gate did not read it, "
                f"and a file it did not read is not a file it found clean"
            )
            continue
        read += 1
        scan = tables_in(path)
        for number, row_id in scan.orphans:
            problems.append(
                f"{_display(path)}:{number}: row {row_id!r} is in no table — no "
                f"header row and separator above it, so GFM renders the whole "
                f"line as text, pipes and all"
            )
        for table in scan.tables:
            tables += 1
            rel = _display(path)
            if args.census:
                print(f"{rel}:{table.header_line}  {table.width} columns, {len(table.rows)} rows")
            for number, count, row_id in table.rows:
                rows += 1
                if count == table.width:
                    continue
                verb = "more" if count > table.width else "fewer"
                lost = (
                    " — GFM drops the excess, so that text is not on the page"
                    if count > table.width else ""
                )
                problems.append(
                    f"{rel}:{number}: row {row_id!r} has {count} cells, {verb} than its "
                    f"header's {table.width} (header at line {table.header_line}){lost}"
                )

    # A GATE THAT FOUND NO TABLES HAS NOT PASSED. `git ls-files` answering
    # nothing, a bad `--paths`, or a fence-tracking bug that swallowed the
    # file would all otherwise print "clean" over an empty census.
    if not tables:
        print("::error::no Markdown tables were found at all — this gate looked at nothing")
        return 1

    if problems:
        annotate = bool(os.environ.get("GITHUB_ACTIONS"))
        for line in problems:
            print(f"::error::{line}" if annotate else line)
        print(
            f"\n{len(problems)} finding(s) across {tables} table(s) "
            f"and {rows} row(s) in {read} Markdown file(s) read."
        )
        return 1
    print(
        f"clean: all {rows} row(s) in {tables} table(s) across {read} "
        f"Markdown file(s) carry their header's cell count"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
