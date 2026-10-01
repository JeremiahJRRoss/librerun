#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The release notes for a tag, from ``CHANGELOG.md`` (blueprint S9).

    python3 scripts/release_notes.py v1.1.0-beta.1 > notes.md

Takes the ``## [<version>]`` section when the changelog has one — its
heading may carry a date, ``## [1.1.0-beta.1] — 2026-10-02`` — and the
``## [Unreleased]`` section when it does not, saying which in the notes
themselves. A release whose notes silently came from somewhere other
than where the reader will look for them is worse than no notes.

Why the fallback exists rather than a rule that the section must be
dated first: batches run in parallel sessions, all of them appending to
``[Unreleased]``, and dating the section at every tag would rewrite a
file other sessions have open. A section is dated when its version is
released, and from then on that version's notes come from it.

A ``#N`` in the notes goes into a code span. A GitHub Release body links
``#N`` to issue N of the repository it is published in, and the
changelog's numbers are the development record's, which the public
repository's issue N is not (L34, L39).
"""
from __future__ import annotations

import itertools
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def section(text: str, heading_re: str) -> tuple[str, str] | None:
    """The body under the first ``## …`` heading matching, and the heading."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(heading_re, line):
            body: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.startswith("## "):
                    break
                body.append(nxt)
            return line.strip(), "\n".join(body).strip("\n")
    return None


# GitHub refuses a release body over 125,000 characters, and refuses it
# at the END of the release job, with nothing to show for the run. The
# cap is well under the limit and leaves room for the header and the footer;
# the changelog at the tag is the full text, and the notes say so rather
# than trailing off.
BODY_LIMIT = 60_000


def cap(body: str, tag: str) -> str:
    if len(body) <= BODY_LIMIT:
        return body
    kept: list[str] = []
    size = 0
    for line in body.splitlines():
        if size + len(line) + 1 > BODY_LIMIT:
            break
        kept.append(line)
        size += len(line) + 1
    kept.append("")
    kept.append(
        f"*… truncated here: the changelog section is longer than a GitHub "
        f"release body may be. `CHANGELOG.md` at `{tag}` has all of it.*"
    )
    return "\n".join(kept)


# A `#N` GitHub would link: not part of a word, a path, an entity or
# another `#`, and ending where the number does.
ISSUE_NUMBER = re.compile(r"(?<![\w&#/])#\d+\b")
# What is never rewritten: a code span — a whole backtick run, up to the
# next run of the same length — a link's destination and an autolink.
PROTECTED = re.compile(
    r"(?<![`\\])(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)|\]\([^)]*\)|<[a-z]+://[^>\s]*>")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
# A block starts at a blank line, a list item or a heading: a code span
# cannot reach across one, so an unclosed backtick in one block never
# pairs with a backtick in the next.
BLOCK_START = re.compile(r"^\s*$|^\s*(?:[-*+]|\d+[.)])\s|^#{1,6}\s")


def _unlinked_block(text: str) -> str:
    pieces: list[tuple[bool, str]] = []
    at = 0
    for kept in PROTECTED.finditer(text):
        pieces += [(False, text[at:kept.start()]), (True, kept.group(0))]
        at = kept.end()
    pieces.append((False, text[at:]))
    # A backtick run outside every span closes none, so it is literal; a
    # span opened by a run of the same length after it would pair with it
    # instead of with its own closing run. The spans made here use a
    # length no such run has.
    loose = {len(run) for kept, piece in pieces if not kept for run in re.findall(r"`+", piece)}
    tick = "`" * next(n for n in itertools.count(1) if n not in loose)
    return "".join(
        piece if kept else ISSUE_NUMBER.sub(lambda m: f"{tick}{m.group(0)}{tick}", piece)
        for kept, piece in pieces
    )


def unlinked(body: str) -> str:
    """Every `#N` outside a code span, a fence or a link, put in a code span."""
    out: list[str] = []
    block: list[str] = []
    fence = ""

    def flush() -> None:
        if block:
            out.append(_unlinked_block("\n".join(block)))
            block.clear()

    for line in body.split("\n"):
        opened = FENCE.match(line)
        if fence:
            out.append(line)
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = ""
            continue
        if opened:
            flush()
            fence = opened.group(1)
            out.append(line)
            continue
        if BLOCK_START.match(line):
            flush()
        block.append(line)
    flush()
    return "\n".join(out)


# What the header calls a pre-release, by its label (D38's scheme, which
# `scripts/check_single_version.py` holds VERSION to).
KINDS = {"alpha": "an alpha", "a": "an alpha", "beta": "a beta", "b": "a beta",
         "rc": "a release candidate"}


def kind_of(version: str) -> str:
    label = re.match(r"[^-]*-([A-Za-z]*)", version)
    return KINDS.get(label.group(1) if label else "", "a pre-release")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: release_notes.py <tag>", file=sys.stderr)
        return 2
    tag = sys.argv[1]
    version = tag[1:] if tag.startswith("v") else tag
    text = (ROOT / "CHANGELOG.md").read_text()

    found = section(text, rf"^## \[{re.escape(version)}\]")
    if found:
        _, body = found
        preamble = ""
    else:
        found = section(text, r"^## \[Unreleased\]")
        if not found:
            print("CHANGELOG.md has neither a section for this version nor "
                  "an [Unreleased] one", file=sys.stderr)
            return 1
        _, body = found
        preamble = (
            f"> These notes are the **[Unreleased]** section of `CHANGELOG.md` as it\n"
            f"> stood at `{tag}`. The changelog has no section for {version} yet: a\n"
            f"> section is dated when its version is released.\n\n"
        )

    body = cap(unlinked(body), tag)
    prerelease = "-" in version
    header = f"## LibreRun {version}\n\n"
    if prerelease:
        header += (
            f"**Pre-release: {kind_of(version)}.** Not for production. It exists to be\n"
            "tested, and the next pre-release may change anything in it, so back up\n"
            "before each upgrade. `docs/platform/Releasing.md` says what a\n"
            "pre-release is for.\n\n"
        )
    footer = (
        "\n\n---\n\n"
        # A release is source only (A2; L37, D26): the footer says how to
        # build it and names no image, registry or owner to pull from.
        "Source only: build with `./scripts/demo.sh`; the CLI and SDK install\n"
        "from the tree. No image, wheel or package is published: the tag's\n"
        "source archive is the release (`docs/platform/Releasing.md`).\n\n"
        "Licensed under AGPL-3.0-only, with four Apache-2.0 directories and the\n"
        "third-party notices `NOTICE` lists (`LICENSE`, `NOTICE`); the name is\n"
        "governed separately (`TRADEMARKS.md`). Contributions come in under a DCO\n"
        "sign-off (`CONTRIBUTING.md`).\n"
    )
    sys.stdout.write(header + preamble + body + footer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
