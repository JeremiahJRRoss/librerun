#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Write ``docs/api/openapi.yaml`` from the live app (blueprint B10, S8).

    python3 scripts/export_openapi.py            # from the repository root
    python3 scripts/export_openapi.py --check    # CI: red when it drifted

A script rather than a one-liner in the spec's own header, for two
reasons the header learned the hard way. Queue-only logging owns stdout
since S4, so the documented ``print(...) > file`` produced an empty
file. And the replacement, which split the existing file on
``openapi: 3.1.0`` to keep the header, then found that string inside the
header — its own command text — and truncated what it was preserving.
The header is the leading comment block instead, which no comment can
imitate.

``--check`` is the S8 gate. The committed document is a **published
interface**: people read it, generate clients from it and diff releases
against it, so "someone regenerates it when they remember" is not a
contract. The check regenerates in memory, compares, and prints a
unified diff of what moved — the same failure mode as
``scripts/export_manifest_reference.py``, and deliberately the same
shape, so a contributor who has seen one recognises the other.
"""
from __future__ import annotations

import argparse
import difflib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "api" / "openapi.yaml"


def real_stderr():
    """A handle on the process's ORIGINAL stderr.

    Importing ``app.main`` installs a process-wide fd capture that
    re-points fd 1 and 2 into the log pipeline — so a report written
    afterwards, even to ``sys.__stderr__``, comes back as structlog
    records with the diff mangled a line at a time and GitHub's
    ``::error::`` prefix buried inside a formatted message. Duplicating
    fd 2 BEFORE the import gives a handle on the file descriptor the
    capture cannot reach. Measured: without this, the drift report was
    one diff line and an annotation CI could not parse.
    """
    return os.fdopen(os.dup(2), "w", encoding="utf-8", closefd=True)


def header_of(text: str) -> str:
    """The comment block a generated spec keeps, verbatim."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.startswith("#"):
            return "".join(lines[:index])
    return text


def render(path: Path) -> str:
    """The document the live app implies, header of ``path`` included."""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.main import app

    import yaml

    header = header_of(path.read_text(encoding="utf-8")) if path.exists() else ""
    return header + yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="do not write; exit 1 if the committed file has drifted")
    ap.add_argument("path", nargs="?", default=str(SPEC),
                    help=f"the document to write or check (default {SPEC.relative_to(ROOT)})")
    args = ap.parse_args(argv)

    path = Path(args.path)
    report = real_stderr()                       # before the import, on purpose
    fresh = render(path)

    if args.check:
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current == fresh:
            report.write(f"{path.relative_to(ROOT)} is current\n")
            report.flush()
            return 0
        report.writelines(difflib.unified_diff(
            current.splitlines(keepends=True), fresh.splitlines(keepends=True),
            fromfile=f"{path.relative_to(ROOT)} (committed)",
            tofile=f"{path.relative_to(ROOT)} (from the live app)",
        ))
        report.write(
            f"\n::error::{path.relative_to(ROOT)} has drifted from the routes the "
            f"app serves. Run `python3 scripts/export_openapi.py` and commit the "
            f"result — the published spec is part of the interface, not a "
            f"by-product of it.\n"
        )
        report.flush()
        return 1

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fresh, encoding="utf-8")
    report.write(f"wrote {path} ({path.stat().st_size} bytes)\n")
    report.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
