#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""publish-purity: nothing in the tree names the development repository.

The public repository is built from this tree (L21, L34), and nothing
LibreRun publishes may name the development repository's owner or its
repository (L39): not the owner's name in any spelling — a URL's slug,
an image path, a handle, an e-mail domain, the bare word in prose, run
together with another word — and not the repository's slug.

The words are never written down here. The check keeps the SHA-256 of
each word (lowercased, with every "-" and "_" removed) and compares it
with every window of the same length in every lowercased alphanumeric
run of every tracked file's contents, path and symlink target, with the
same characters removed. A digest does not hide a word from someone who
already guesses it; it keeps this file, and the tree published from it,
from spelling it. A match prints WHERE, never what, so that the CI log
does not spell it either.

    python3 scripts/check_purity.py              # the tracked tree
    python3 scripts/check_purity.py --text STR   # one string

Exit status 1 on a match, 0 when clean. The canary is a harmless word
whose digest sits beside the real ones, so that the probes
(`scripts/licensing_probes.sh purity`) prove the mechanism without
spelling a kept name; in the development repository they also plant the
names derived from `$GITHUB_REPOSITORY` at run time.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

KEPT = {
    "e25597e10030922f85bb1b6846c6bacd61515158340b708b8ffd4a4ace0011e9": "the development repository's owner",
    "422b7950e94b4bb1e6bd4315cadcab15a1f5217a2da9bacb2da82d47dbd8fa25": "the development repository's slug",
    "ecb4a3bec632c46c3517c4cdf637d543e9e08331645a1a3e15ea8e4953a0a822": "the probe canary",
}
# The length of each kept word, so that only windows that could match are
# hashed. Lengths say nothing a reader could use: many words share them.
LENGTHS = (6, 12, 14)

RUN = re.compile(r"[a-z0-9]+")


def _normalise(text: str) -> str:
    return text.lower().replace("-", "").replace("_", "")


def matches(text: str, cache: dict[str, str | None] | None = None) -> set[str]:
    """The labels of the kept words that occur in text. The cache maps a
    window to its verdict, so a window is hashed once per file and still
    reported every time it occurs."""
    cache = {} if cache is None else cache
    hits: set[str] = set()
    for m in RUN.finditer(_normalise(text)):
        run = m.group(0)
        for n in LENGTHS:
            for i in range(len(run) - n + 1):
                window = run[i:i + n]
                if window not in cache:
                    cache[window] = KEPT.get(hashlib.sha256(window.encode()).hexdigest())
                if cache[window]:
                    hits.add(cache[window])
    return hits


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True).stdout


def scan_tree() -> list[str]:
    root = Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()).decode().strip())
    found: list[str] = []
    entries = _git("ls-files", "-s", "-z", cwd=root).decode().split("\0")
    for entry in filter(None, entries):
        meta, path = entry.split("\t", 1)
        mode, sha = meta.split()[:2]
        for label in matches(path):
            found.append(f"{path}: the PATH names {label}")
        if mode == "120000":
            # A symlink's target is its blob. Read it from the index, so the
            # check holds where symlinks are materialised as plain files.
            target = _git("cat-file", "blob", sha, cwd=root).decode("utf-8", "replace")
            for label in matches(target):
                found.append(f"{path}: its SYMLINK TARGET names {label}")
            continue
        p = root / path
        if not p.is_file():
            continue
        data = p.read_bytes()
        text = data.decode("latin-1") if b"\0" in data[:8000] else data.decode("utf-8", "replace")
        cache: dict[str, str | None] = {}
        for n, line in enumerate(text.splitlines(), 1):
            for label in matches(line, cache):
                found.append(f"{path}:{n}: names {label}")
    return found


def main(argv: list[str]) -> int:
    if argv[:1] == ["--text"]:
        if len(argv) != 2:
            print("usage: check_purity.py --text STRING", file=sys.stderr)
            return 2
        hits = matches(argv[1])
        for label in sorted(hits):
            print(f"the text names {label}")
        return 1 if hits else 0
    if argv:
        print("usage: check_purity.py [--text STRING]", file=sys.stderr)
        return 2
    found = scan_tree()
    if found:
        for f in found:
            print(f"::error::{f}")
        print("::error::The tree names the development repository or its owner (above). L39: nothing "
              "LibreRun publishes names it, in any spelling. Remove the reference; a document that needs "
              "the public repository's URL names github.com/JeremiahJRRoss/librerun instead.")
        return 1
    print("clean: no tracked file, path or symlink target names the development repository or its owner")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
