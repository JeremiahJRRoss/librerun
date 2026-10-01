#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""D12: the README alone is enough, and every command in it is real.

    python3 scripts/readme_walk.py              # from the repository root
    python3 scripts/readme_walk.py --readme X   # check a copy instead

The delight gate's last row (blueprint §3.2, D12) says "the README alone
was enough; every command in it is copy-pasteable". A person walking
D1–D8 from a clean machine is the real test and it belongs to S10, which
runs the gate on two of them. What a machine can do is everything that
person would otherwise discover the hard way, and do it on every pull
request:

1. **Every command resolves.** A shell command in the README that names
   a script, a path or a compose file gets that file checked. A
   ``librerun`` command gets its verb and every flag checked against the
   CLI's own parser — imported, not grepped, so a renamed flag cannot
   pass by looking similar.
2. **Every ``docs/`` path the README mentions exists**, in prose as well
   as in code, because a reader follows both.
3. **D1–D8 are each instructed.** Not "a heading exists": the specific
   instruction the step needs — the boot command, the printed
   credentials, the sample chip, the gate, the report, the trace link,
   the five agents, the scaffold — has to be findable in the text. A
   README that drops the approval step still boots a stack and still
   reads well, and the walker would stall at D4 with nothing to do.

It is offline and reads no state, so it can run in the docs workflow
beside the link checker.
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FENCE = re.compile(r"^```+\s*(\w+)?\s*$")

# D3–D6 are one continuous walk, so they are checked as one: each marker
# must appear AFTER the previous one. Checking them independently is what
# the first version did, and it passed a README with the approval step
# deleted — because "approve" also appears three sections later, in the
# list of what 1.0 does not do. Order is what makes the check mean
# "these instructions are here, in the sequence a person follows".
WALK: list[tuple[str, str, str]] = [
    ("D1", r"git clone\s+\S+", "the clone command"),
    ("D1", r"(\./scripts/demo\.sh|librerun demo)", "a command that boots the stack"),
    ("D1", r"localhost:3000", "where to point the browser"),
    ("D2", r"printed credentials", "that the credentials are printed"),
    ("D3", r"New Run", "how to start a run"),
    ("D3", r"Try a sample", "the sample chip, by the name the page gives it"),
    ("D3", r"submit", "that the sample gets submitted"),
    ("D4", r"\bApprove\b", "the approval step, where the run pauses"),
    ("D5", r"\breport\b", "that a report is produced"),
    ("D6", r"View trace", "the trace link, by the name the page gives it"),
]

# The rest: facts a reader needs, wherever the page states them.
FACTS: list[tuple[str, str, str]] = [
    ("D2", r"banner", "that a banner announces demo mode"),
    ("D4", r"\bgate\b", "that a gate sits between phases"),
    ("D5", r"\bPDF\b", "that the report exports as PDF"),
    ("D6", r"\bJaeger\b", "which trace viewer is in the box"),
    ("D7", r"VITA", "the demo agent"),
    ("D7", r"\becho\b", "the echo reference agent"),
    ("D7", r"LangGraph", "the LangGraph example"),
    ("D7", r"LlamaIndex", "the LlamaIndex example"),
    ("D7", r"Vercel AI SDK", "the Vercel AI SDK example"),
    ("D8", r"pipx install", "how to install the CLI"),
    ("D8", r"librerun init \S+ --template langgraph", "the scaffold command"),
    ("D8", r"librerun up", "how to start the new agent"),
    ("D8", r"librerun battery", "how to know the agent is done"),
]

# Words that open a shell command but are not the command itself:
# a wrapper, or a one-shot `NAME=value` assignment.
PREFIXES = {"sudo", "env", "time", "exec", "nohup"}
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def code_blocks(text: str):
    """Yield ``(language, body)`` for every fenced block."""
    language, body, inside = None, [], False
    for line in text.splitlines():
        m = FENCE.match(line)
        if m and not inside:
            language, body, inside = (m.group(1) or ""), [], True
        elif m and inside:
            yield language, "\n".join(body)
            inside = False
        elif inside:
            body.append(line)


def cli_surface():
    """The verbs and flags the ``librerun`` parser really accepts."""
    sys.path.insert(0, str(ROOT / "cli" / "src"))
    from librerun.cli import build_parser

    import argparse as ap

    def walk(parser, path: tuple[str, ...], out: dict):
        flags = {s for a in parser._actions for s in a.option_strings}
        out[path] = flags
        for action in parser._actions:
            if isinstance(action, ap._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, path + (name,), out)

    surface: dict[tuple[str, ...], set[str]] = {}
    walk(build_parser(), (), surface)
    return surface


def check_command(words: list[str], surface, problems: list[str], where: str) -> None:
    """One shell command from the README."""
    while words and (words[0] in PREFIXES or ASSIGNMENT.match(words[0])):
        words = words[1:]
    if not words:
        return
    head = words[0]

    if head == "librerun":
        rest = [w for w in words[1:]]
        path: tuple[str, ...] = ()
        while rest and not rest[0].startswith("-") and (path + (rest[0],)) in surface:
            path += (rest[0],)
            rest = rest[1:]
        if not path:
            problems.append(f"{where}: `librerun {' '.join(words[1:2])}` is not a command the CLI has")
            return
        allowed = surface[path] | surface[()]
        for word in rest:
            if word.startswith("-") and word != "--":
                flag = word.split("=", 1)[0]
                if flag not in allowed:
                    problems.append(
                        f"{where}: `librerun {' '.join(path)}` has no flag `{flag}`"
                    )
        return

    # A local script or path: ./x, scripts/x, docs/x — but not a flag,
    # a URL, a variable or a binary that comes from the system.
    for word in words:
        if word.startswith(("./", "../")) or re.match(r"^(scripts|docs|backend|frontend|cli|config|services|sdk)/", word):
            candidate = word.split("=", 1)[-1].rstrip(",.;:")
            if not (ROOT / candidate).exists():
                problems.append(f"{where}: `{candidate}` does not exist")


def main(argv: list[str] | None = None) -> int:
    ap_ = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap_.add_argument("--readme", default=str(ROOT / "README.md"),
                     help="the README to check (default: the repository's)")
    args = ap_.parse_args(argv)

    readme = Path(args.readme)
    text = readme.read_text(encoding="utf-8")
    problems: list[str] = []

    # 1 — every command in every shell block.
    surface = cli_surface()
    for language, body in code_blocks(text):
        if language not in ("bash", "sh", "shell", "console", ""):
            continue
        for number, raw in enumerate(body.splitlines(), start=1):
            line = raw.split("#", 1)[0].strip()
            if not line or line.startswith(("$", ">")):
                continue
            for part in re.split(r"\s*(?:&&|\|\||;)\s*", line):
                if not part.strip():
                    continue
                try:
                    words = shlex.split(part)
                except ValueError:
                    continue
                check_command(words, surface, problems, f"{readme.name} shell line {number}")

    # 2 — every docs/ or scripts/ path the README names, in prose as well
    # as in code. Prose matters as much as a command here: "see
    # docs/platform/Install.md" is an instruction to go look, and a reader
    # who cannot find it has been sent nowhere.
    for path in sorted(set(re.findall(r"\b((?:docs|scripts)/[\w./-]*[\w])", text))):
        if not (ROOT / path.rstrip(".")).exists():
            problems.append(f"{readme.name}: mentions `{path}`, which does not exist")

    # 3 — D1–D8 are each instructed, and the walk is in order.
    cursor, previous = 0, "the start of the file"
    for step, pattern, what in WALK:
        m = re.compile(pattern, re.I).search(text, cursor)
        if m is None:
            problems.append(
                f"{readme.name}: {step} cannot be completed from this README — "
                f"it does not say {what}, after {previous} (no match for "
                f"/{pattern}/ from offset {cursor})"
            )
        else:
            cursor, previous = m.end(), what
    for step, pattern, what in FACTS:
        if not re.search(pattern, text, re.I):
            problems.append(
                f"{readme.name}: {step} cannot be completed from this README — "
                f"it does not say {what} (no match for /{pattern}/)"
            )

    if problems:
        annotate = "::error::" if os.environ.get("GITHUB_ACTIONS") else ""
        for line in problems:
            print(f"{annotate}{line}")
        print(f"\n{len(problems)} problem(s): the README is not enough on its own (D12).")
        return 1
    print(f"clean: every command in {readme.name} resolves, and D1–D8 are each instructed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
