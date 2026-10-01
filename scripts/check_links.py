#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Every relative link in a tracked Markdown file points at something real.

    python3 scripts/check_links.py              # from the repository root
    python3 scripts/check_links.py --list       # print the files it reads

Blueprint S8: the documentation was reorganised into ``docs/platform/``,
``docs/authoring/``, ``docs/agents/`` and ``docs/api/`` with a redirect
stub at every old path. A reorganisation is exactly the change that
silently breaks links, so the tree gets a checker rather than a promise.

**It is deliberately offline.** It never resolves ``http(s)://`` — a
gate that calls the internet fails when somebody else's server is down,
and a gate that goes red for reasons unrelated to the diff is a gate
people learn to re-run rather than read. What it does check is the half
that is ours and that actually rots: paths inside this repository, and
the heading anchors they point at.

Scope is every tracked ``*.md`` — ``README.md``, ``CLAUDE.md``,
``CHANGELOG.md``, all of ``docs/`` including ``docs/release/`` and the
executive plan, and the Markdown under ``backend/``, ``cli/``, ``sdk/``
and ``scripts/``.

Exit code 0 when every link resolves, 1 when any does not; every broken
link is printed with its file, line and reason.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent

# A fenced block shows links as examples; an inline code span quotes a
# path as text. Neither is a link, and checking them produces failures
# that can only be silenced by rewriting prose. Both are blanked (not
# deleted) so that line numbers in a report still match the file.
FENCE = re.compile(r"^(?P<indent>\s{0,3})(?P<fence>```+|~~~+)(?P<info>.*)$")
CODE_SPAN = re.compile(r"`+[^`\n]*`+")

# [text](target) and [text](target "title"), plus the angle-bracket form
# [text](<target with spaces>). The text may itself contain brackets
# (`[`foo`](bar)`), so it is matched lazily and nesting is not supported
# — a link label with an unbalanced bracket is rare enough to skip.
INLINE = re.compile(r"(?<!\!)\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(<[^>\n]*>|[^()\s]*(?:\([^()\s]*\)[^()\s]*)*)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)")
# An image is checked the same way; only the leading ! differs.
IMAGE = re.compile(r"\!\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(\s*(<[^>\n]*>|[^()\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^()]*\)))?\s*\)")
# A reference definition: [label]: target "title"
REFDEF = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>\n]*>|\S+)")

ATX = re.compile(r"^(?P<indent>\s{0,3})(?P<hashes>#{1,6})\s+(?P<text>.*?)\s*#*\s*$")
SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
HTML_ANCHOR = re.compile(r"""<a\s[^>]*?(?:name|id)\s*=\s*["']([^"']+)["']""", re.I)
HTML_ID = re.compile(r"""<(?!a\s)[a-zA-Z][^>]*?\sid\s*=\s*["']([^"']+)["']""")


def blank_fences(lines: list[str]) -> list[str]:
    """Return ``lines`` with fenced blocks blanked out, line count kept.

    Blanked rather than dropped so a reported line number still matches
    the file a reader opens.
    """
    out: list[str] = []
    fence: str | None = None
    for line in lines:
        if fence is None:
            m = FENCE.match(line)
            if m and "`" not in m.group("info"):
                fence = m.group("fence")[0] * 3
                out.append("")
                continue
            out.append(line)
        else:
            out.append("")
            m = FENCE.match(line)
            if m and m.group("fence")[0] * 3 == fence and not m.group("info").strip():
                fence = None
    return out


def blank_code(lines: list[str]) -> list[str]:
    """``blank_fences`` plus inline code spans, for finding links.

    Headings do NOT go through this one: a heading whose whole text is a
    code span (``## `AgentManifest` ``, which is how the generated
    manifest reference writes every one of its fifteen) would blank to
    nothing and its anchor would read as missing — the false failure
    this function was caught producing the first time it ran.
    """
    return [CODE_SPAN.sub(lambda s: " " * len(s.group(0)), line)
            for line in blank_fences(lines)]


def slug(text: str) -> str:
    """GitHub's heading slug: the rules the anchors in this tree rely on."""
    text = re.sub(r"<[^>]+>", "", text)                       # inline HTML
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)     # links/images
    text = re.sub(r"`+", "", text)                             # code spans
    text = re.sub(r"[*_~]", "", text)                          # emphasis
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text, flags=re.UNICODE)     # punctuation
    return text.replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    """Every fragment ``path`` offers: heading slugs and explicit ids."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return set()
    found: set[str] = set()
    for line in raw:                       # explicit ids survive in code too
        found.update(a.lower() for a in HTML_ANCHOR.findall(line))
        found.update(a.lower() for a in HTML_ID.findall(line))
    lines = blank_fences(raw)
    seen: dict[str, int] = {}

    def add(text: str) -> None:
        base = slug(text)
        if not base:
            return
        n = seen.get(base, 0)
        seen[base] = n + 1
        found.add(base if n == 0 else f"{base}-{n}")

    for index, line in enumerate(lines):
        m = ATX.match(line)
        if m:
            add(m.group("text"))
            continue
        # Setext: the underline belongs to the non-blank line above it.
        if SETEXT.match(line) and index and lines[index - 1].strip():
            if not ATX.match(lines[index - 1]):
                add(lines[index - 1].strip())
    return found


def targets(lines: list[str]):
    """Yield ``(line_number, raw_target)`` for every link in ``lines``."""
    for number, line in enumerate(blank_code(lines), start=1):
        for pattern in (IMAGE, INLINE):
            for m in pattern.finditer(line):
                yield number, m.group(1)
        m = REFDEF.match(line)
        if m:
            yield number, m.group(1)


def tracked_markdown() -> list[Path]:
    """Every tracked Markdown file, each of them ONCE.

    `git ls-files` lists a path once per index STAGE, so during an
    unresolved merge a conflicted file appears three times — and this
    checker then reads it three times and reports each broken link in it
    three times, while the "how many files did you look at" number in
    the verdict silently inflates. Measured twice for real: mid-merge
    this said 63 files, then 61, where the tree has 59.

    CI never runs mid-merge, so this was never going to be a red build.
    What it would do is make the one number a reader uses to judge how
    much was examined unreliable — which is the number that caught the
    orphaned rows in `check_tables.py` (§12 211). Order is preserved so
    the output stays stable.
    """
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md", "*.markdown"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    ).stdout
    return [ROOT / name for name in dict.fromkeys(n for n in out.split("\0") if n)]


def check(path: Path, cache: dict[Path, set[str]]) -> list[str]:
    rel = path.relative_to(ROOT)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    problems: list[str] = []
    own: set[str] | None = None

    for number, raw in targets(lines):
        target = raw.strip()
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        if not target:
            continue
        parsed = urlparse(target)
        if parsed.scheme:                       # http(s), mailto, ftp, …
            continue
        if target.startswith("//"):             # protocol-relative
            continue

        fragment = unquote(parsed.fragment).lower()
        where = unquote(parsed.path)

        if not where:                           # same-file anchor
            if not fragment:
                continue
            if own is None:
                own = anchors_of(path)
            if fragment not in own:
                problems.append(
                    f"{rel}:{number}: no such anchor '#{fragment}' in this file"
                )
            continue

        # A target is relative to the file that writes it. An absolute
        # one (leading '/') is repository-root relative, which is how a
        # link rendered by a docs site would read.
        base = ROOT if where.startswith("/") else path.parent
        resolved = (base / where.lstrip("/")).resolve()
        try:
            resolved.relative_to(ROOT)
        except ValueError:
            problems.append(f"{rel}:{number}: '{target}' escapes the repository")
            continue
        if not resolved.exists():
            problems.append(f"{rel}:{number}: '{target}' does not exist")
            continue
        if fragment and resolved.suffix.lower() in (".md", ".markdown"):
            if resolved not in cache:
                cache[resolved] = anchors_of(resolved)
            if fragment not in cache[resolved]:
                problems.append(
                    f"{rel}:{number}: '{target}' — no anchor "
                    f"'#{fragment}' in {resolved.relative_to(ROOT)}"
                )
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", action="store_true",
                    help="print the files that would be checked, and stop")
    ap.add_argument("paths", nargs="*",
                    help="check these files instead of every tracked one")
    args = ap.parse_args(argv)

    files = [Path(p).resolve() for p in args.paths] if args.paths else tracked_markdown()
    if args.list:
        for path in files:
            print(path.relative_to(ROOT))
        return 0

    cache: dict[Path, set[str]] = {}
    problems: list[str] = []
    for path in files:
        if path.exists():
            problems.extend(check(path, cache))

    if problems:
        annotate = bool(os.environ.get("GITHUB_ACTIONS"))
        for line in problems:
            print(f"::error::{line}" if annotate else line)
        print(f"\n{len(problems)} broken link(s) across {len(files)} Markdown file(s).")
        return 1
    print(f"clean: every relative link in {len(files)} Markdown file(s) resolves")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
