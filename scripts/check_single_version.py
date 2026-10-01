#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""One VERSION, asserted across every place the tree writes it down (S9).

The root ``VERSION`` file is the single source. Nothing derives its value
at runtime — the backend image does not contain that file, and a constant
that reads a missing file at import time is a boot failure waiting for
the first operator who copies a directory somewhere else. Every artifact
writes the version down, and this check asserts the copies agree.

Two spellings, because two ecosystems disagree and neither is wrong:

  * ``VERSION`` holds the canonical **semver** spelling, ``1.1.0-beta.1``.
    So do ``package.json`` and its lock's two root lines, and the version
    constants of the two services that are NOT pip distributions (the
    backend and the gateway are copied into images, not installed).
  * A Python **distribution** — its ``pyproject.toml`` and the
    ``__version__`` its package exports — holds the canonical **PEP 440**
    spelling, ``1.1.0b1``. `pip` normalises ``1.1.0-beta.1`` to that
    anyway; writing the normalised form is how `pip show` and
    `librerun --version` end up saying the same thing.

The converter between the two is a table (D38): ``alpha`` and ``a`` are
``a``, ``beta`` and ``b`` are ``b``, ``rc`` is ``rc``, and the hyphen and
the dot go — ``1.1.0-beta.1`` is ``1.1.0b1``, ``1.0.0-rc1`` is
``1.0.0rc1``. Any other pre-release stops the check and names what it
met, rather than writing a spelling pip rewrites: ``1.1.0beta.1`` and
``1.1.0-preview.1`` are both valid PEP 440, and pip prints them as
``1.1.0b1`` and ``1.1.0rc1``.

One place the version appears is deliberately NOT listed here:
``docs/api/openapi.yaml``, whose ``info.version`` is the backend's
constant as the running app reports it. It is generated, and the
``openapi-drift`` gate (S8) compares the whole committed document
against the live app — a stronger check than one line, and the one that
catches a hand-edit. Asserting it here too would be a second opinion
that can disagree with the first.

The unlisted-file rule is the other half. Asserting a fixed list keeps
the listed files honest and says nothing about a package added next
month, so every ``pyproject.toml`` and ``package.json`` in the tree must
be either pinned to VERSION or exempt **by name, with a reason**. A new
package fails this check until someone decides which it is. That is the
point: the failure is the decision being asked for.

Run it here, or as the ``single-version`` job of
``.github/workflows/release-readiness.yml``:

    python3 scripts/check_single_version.py
    python3 scripts/check_single_version.py --print   # just the values
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Pinned to VERSION: every distribution and service the tree builds.
# ---------------------------------------------------------------------------
# kind: "pep440" for a Python distribution, "semver" for everything else.
PINNED: list[tuple[str, str, str, str]] = [
    # (path, kind, regex with one capture group, what it is)
    ("cli/pyproject.toml", "pep440", r'^version = "([^"]+)"$',
     "the librerun CLI, installed from the tree"),
    ("cli/src/librerun/__init__.py", "pep440", r'^__version__ = "([^"]+)"$',
     "what `librerun --version` prints"),
    ("sdk/python/librerun-agent/pyproject.toml", "pep440", r'^version = "([^"]+)"$',
     "librerun-agent, the SDK, installed from the tree"),
    ("sdk/python/librerun-agent/src/librerun_agent/__init__.py", "pep440",
     r'^__version__ = "([^"]+)"$', "the SDK's exported version"),
    ("backend/adapters/pyproject.toml", "pep440", r'^version = "([^"]+)"$',
     "librerun-langgraph, the adapter, installed from the tree"),
    ("backend/app/version.py", "semver", r'^__version__ = "([^"]+)"$',
     "the backend image: /api/v1/meta and the OTel service.version"),
    ("services/gateway/gateway/version.py", "semver", r'^__version__ = "([^"]+)"$',
     "the gateway image: its OpenAPI version"),
    ("frontend/package.json", "semver", r'^  "version": "([^"]+)",$',
     "the librerun-web image"),
    # npm writes package.json's version into its lock twice, at the root
    # and on the root package. They were once exempt as "npm keeps them in
    # step" and said 0.1.0 for a whole release until K4b rewrote them, so
    # both are read: each pattern is anchored to its own block, because
    # every locked package has a "version" line of its own.
    ("frontend/package-lock.json", "semver",
     r'\A\{\n  "name": "[^"]+",\n  "version": "([^"]+)",$',
     "the web UI's lock, its root"),
    ("frontend/package-lock.json", "semver",
     r'^  "packages": \{\n    "": \{\n      "name": "[^"]+",\n      "version": "([^"]+)",$',
     "the web UI's lock, its root package"),
]

# ---------------------------------------------------------------------------
# Exempt, by name, with the reason. Not a convenience list: each line is a
# decision that a reviewer can disagree with.
# ---------------------------------------------------------------------------
EXEMPT: dict[str, str] = {
    "backend/agents/vita_v1/pyproject.toml":
        "the bundled demo agent. An agent is a PLUG-IN with its own release "
        "line, and the chassis special-casing one is the thing L13 forbids. "
        "Tying its version to the platform's would say the two ship together "
        "forever, which is exactly what a pluggable agent is not.",
    "backend/agents/_examples/vercel_ai_answer_ts/package.json":
        "an example agent, versioned as the example it is; it is built from "
        "the checkout like every image, and nothing tags it from here.",
    "cli/src/librerun/templates/container-ts/package.json":
        "a TEMPLATE. `librerun init` copies it into somebody else's agent, "
        "which starts at its own 0.1.0 and has no business inheriting the "
        "platform's version.",
}

# D38: each SemVer pre-release label this project uses, and the letter
# PEP 440's canonical form writes for it. Canonical is what `pip show`
# prints. PEP 440 reads more labels than these — `c`, `pre` and `preview`
# as `rc`, `dev` as a development release — and pip prints each respelled,
# so one outside the table is refused rather than guessed at.
PEP440_LABELS = {"alpha": "a", "a": "a", "beta": "b", "b": "b", "rc": "rc"}
# One label and one number, the dot between them optional: `beta.1`,
# `rc1`. The number has no leading zero, which SemVer forbids and pip
# would drop.
PRERELEASE = re.compile(r"(?P<label>[A-Za-z]+)\.?(?P<number>0|[1-9][0-9]*)")


class OutsideTheScheme(ValueError):
    """A pre-release with no canonical PEP 440 spelling in the table."""


def pep440_of(semver: str) -> str:
    """The canonical PEP 440 form of a semver version (D38).

    ``1.1.0-beta.1`` -> ``1.1.0b1``, ``1.0.0-rc1`` -> ``1.0.0rc1``,
    ``1.1.0-alpha.2`` -> ``1.1.0a2``; a plain ``1.0.0`` is unchanged.
    Anything else after the hyphen raises ``OutsideTheScheme``, naming it.
    """
    release, hyphen, prerelease = semver.partition("-")
    if not hyphen:
        return release
    found = PRERELEASE.fullmatch(prerelease)
    if not found:
        raise OutsideTheScheme(
            f"its pre-release {prerelease!r} is not one label and one number "
            f"(`beta.1`, `rc1`), so it has no canonical PEP 440 spelling. "
            f"The scheme is alpha or a, beta or b, and rc, each with a number."
        )
    label = found["label"]
    if label not in PEP440_LABELS:
        raise OutsideTheScheme(
            f"its pre-release label {label!r} is outside the scheme: alpha or "
            f"a, beta or b, and rc, each with a number (`1.1.0-beta.1` is "
            f"`1.1.0b1`). PEP 440 reads other labels too, and pip prints them "
            f"respelled, so a distribution would carry one version and "
            f"`pip show` print another. Use a label in the scheme."
        )
    return f"{release}{PEP440_LABELS[label]}{found['number']}"


def read_version() -> str:
    version = (ROOT / "VERSION").read_text().strip()
    if not version:
        raise SystemExit("VERSION is empty")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?", version):
        raise SystemExit(
            f"VERSION is {version!r}, which is not a semver release: "
            "MAJOR.MINOR.PATCH with an optional -prerelease."
        )
    return version


def declared(path: Path, pattern: str) -> str | None:
    found = re.search(pattern, path.read_text(), flags=re.M)
    return found.group(1) if found else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print", action="store_true", dest="print_only",
                        help="print VERSION and the two spellings, and exit")
    args = parser.parse_args()

    semver = read_version()
    try:
        pep440 = pep440_of(semver)
    except OutsideTheScheme as refused:
        print(f"::error::VERSION is {semver!r}: {refused}")
        return 1
    if args.print_only:
        print(f"semver={semver}")
        print(f"pep440={pep440}")
        return 0

    print(f"VERSION = {semver}   (PEP 440: {pep440})")
    failures: list[str] = []

    for rel, kind, pattern, what in PINNED:
        path = ROOT / rel
        want = pep440 if kind == "pep440" else semver
        if not path.exists():
            failures.append(
                f"{rel}: pinned to VERSION but missing. If the artifact is "
                f"gone, delete its line in scripts/check_single_version.py "
                f"and say so in the pull request — do not leave the check "
                f"asserting nothing."
            )
            continue
        got = declared(path, pattern)
        if got is None:
            failures.append(
                f"{rel}: no version line matched {pattern!r}. The file moved "
                f"its version, and this check has been reading nothing ever "
                f"since — fix the pattern, not the file."
            )
        elif got != want:
            failures.append(
                f"{rel}: says {got!r}, VERSION says {want!r} ({what}). "
                f"Bump both, or bump VERSION."
            )
        else:
            print(f"  ok  {rel:<58} {got}   {what}")

    # Every package manifest in the tree is pinned or exempt by name.
    pinned_paths = {rel for rel, *_ in PINNED}
    # `-z` and a NUL split, then dedup — two defects in one line.
    # The default output is COOKED: a path holding a space survives
    # `.split()` as two paths that name no file, and one holding a
    # non-ASCII character is printed quoted and octal-escaped
    # (`"a/caf\303\251.json"`), which names none either — so a manifest
    # could sit in the tree neither pinned nor exempt and never be
    # reported. And `git ls-files` lists a path once per index STAGE, so
    # mid-merge a conflicted manifest is accused three times.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--",
         "*pyproject.toml", "*package.json", "*package-lock.json"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    for rel in sorted(dict.fromkeys(r for r in tracked if r)):
        if rel in pinned_paths or rel in EXEMPT:
            continue
        failures.append(
            f"{rel}: a package manifest that is neither pinned to VERSION nor "
            f"exempt. Decide which it is in scripts/check_single_version.py: "
            f"add it to PINNED if it carries the platform's version, or to EXEMPT with "
            f"the reason it has a version of its own."
        )
    for rel in sorted(EXEMPT):
        if rel not in tracked:
            failures.append(
                f"{rel}: listed as EXEMPT but not tracked. A stale exemption "
                f"is a hole waiting for a file with that name — remove it."
            )
        else:
            print(f"  --  {rel:<58} exempt")

    if failures:
        print()
        for line in failures:
            print(f"::error::{line}")
        print(f"\n{len(failures)} version problem(s).")
        return 1
    print("\nOne VERSION, everywhere it is written down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
