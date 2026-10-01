#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""R14 — every image the tree builds from, runs or tests on is pinned (L38, D25).

A tag names whatever its publisher last pushed under it, so a tag alone is
a promise the publisher can change without the tree changing. Each image
reference therefore carries the digest of the multi-arch index its tag
pointed to when it was last reviewed, and moves only by a reviewed refresh
(``scripts/refresh_image_digests.py``). This reads every tracked
Dockerfile or Containerfile, every compose file however it is named
(``compose.yaml``, ``agents.compose.yaml``, ``docker-compose.yml``), the
compose fragment ``librerun init`` appends to an operator's
``agents.compose.yaml``, and every workflow, comments stripped, and exits
1 naming file, line and rule for:

``images``
  * a ``FROM`` (stage aliases and ``scratch`` aside), a compose ``image:``
    (a ``${LIBRERUN_IMAGE_PREFIX…}`` build aside: those are this project's
    own images, and R12 proves each one is built, never pulled), a
    workflow ``image:`` (a local ``librerun-…`` matrix value aside) or a
    workflow ``…_IMAGE:`` value that is not fully qualified (a short name
    resolves through whatever search list the engine is configured with),
    has no tag or the tag ``latest``, or has no ``sha256`` digest of 64
    hex digits;
  * a ``docker`` or ``podman`` ``run``, ``create`` or ``pull`` in a
    workflow that names its image by anything but a variable — a
    ``…_IMAGE`` one, or a ``${{ matrix.… }}`` whose values are read where
    they are written. A compose verb naming a service (``pull edge``)
    inherits compose's pinned reference and passes.

``matrix``
  * an image reference whose ``name:tag``, or a tracked lock whose path,
    is in no row of ``docs/release/Distribution_Surface_Matrix.md``: a
    code span of a row's Component cell, compared whole, so a row for one
    tag never stands in for a tag it begins with;
  * a matrix whose LiteLLM row does not hold the gateway lock's LiteLLM
    version and the licence ``THIRD_PARTY.md`` records for it.

It fails having read nothing, and names what it read when it passes.

    python3 scripts/check_dependency_identity.py              # every rule
    python3 scripts/check_dependency_identity.py images       # one rule
    python3 scripts/check_dependency_identity.py --root DIR   # another checkout

Standard library only. ``scripts/dependency_identity_probes.sh`` plants
each violation, sees this go red, and puts the tree back.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MATRIX = "docs/release/Distribution_Surface_Matrix.md"
GATEWAY_LOCK = "services/gateway/requirements.lock.txt"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# This project's own images: built from the tree, never pulled (R12).
FIRST_PARTY_COMPOSE = "${LIBRERUN_IMAGE_PREFIX"
LOCAL_MATRIX_IMAGE = re.compile(r"librerun-[a-z0-9][a-z0-9._-]*")
# The expressions an image may be named by in a workflow: a matrix value,
# which this reads where the matrix writes it, or an ``…_IMAGE`` variable.
EXPRESSION_IMAGE = re.compile(r"\$\{\{\s*(?:matrix\.[\w.-]+|env\.\w*_IMAGE)\s*\}\}")
SHELL_IMAGE = re.compile(r"\$(?:\{\w*_IMAGE\}|\w*_IMAGE)")
LOCK_NAMES = re.compile(r"(?:requirements\.lock\.txt|package-lock\.json|build-constraints\.txt)")
# What an engine builds from, by the names Docker and Podman read: a
# ``Dockerfile`` or ``Containerfile``, with or without a suffix or prefix.
BUILD_FILE = re.compile(r"(^|/)(?:(?:Docker|Container)file[^/]*|[^/]+\.(?:[Dd]ocker|[Cc]ontainer)file)$")
# A compose file, however it is named: ``compose.yaml``,
# ``agents.compose.yaml``, ``docker-compose.yml``, ``compose.override.yaml``.
COMPOSE_FILE = re.compile(r"(^|/)(?:[^/]*[.-])?compose(?:[.-][^/]*)?\.ya?ml$")
# Compose by content, not by name: the service ``librerun init`` renders
# into an operator's ``agents.compose.yaml``. A fragment that moved is an
# error, never a file silently no longer read.
COMPOSE_FRAGMENTS = ("cli/src/librerun/templates/_fragment.yaml",)

# ``docker run`` / ``create`` options that take the next word as their
# value when it is not joined by ``=``. An option this does not know is
# read as a flag, so a value it would have skipped is read as the image
# and reported, never passed.
RUN_VALUE_OPTIONS = frozenset(
    """
    -a --attach --add-host --annotation --blkio-weight --blkio-weight-device
    --cap-add --cap-drop --cgroup-parent --cgroupns --cidfile -c --cpu-shares
    --cpu-period --cpu-quota --cpu-rt-period --cpu-rt-runtime --cpus
    --cpuset-cpus --cpuset-mems --device --device-cgroup-rule
    --device-read-bps --device-read-iops --device-write-bps
    --device-write-iops --dns --dns-option --dns-search --domainname -e --env
    --env-file --entrypoint --expose --gpus --group-add --health-cmd
    --health-interval --health-retries --health-start-period
    --health-start-interval --health-timeout -h --hostname --ip --ip6 --ipc
    --isolation --kernel-memory -l --label --label-file --link
    --link-local-ip --log-driver --log-opt --mac-address -m --memory
    --memory-reservation --memory-swap --memory-swappiness --mount --name
    --network --net --network-alias --oom-score-adj --pid --pids-limit
    --platform -p --publish --pull --restart --runtime --security-opt
    --shm-size --stop-signal --stop-timeout --storage-opt --sysctl --tmpfs
    --ulimit -u --user --userns --uts -v --volume --volume-driver
    --volumes-from -w --workdir
    """.split()
)
PULL_VALUE_OPTIONS = frozenset({"--platform"})
# ``docker`` / ``podman`` options before the subcommand that take a value.
GLOBAL_VALUE_OPTIONS = frozenset(
    {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level", "--tlscacert",
     "--tlscert", "--tlskey", "--connection", "--url", "--identity", "--root", "--runroot"}
)
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "{", "}", "then", "do", "!"})


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    text: str
    what: str


def build_files(files: list[str]) -> list[str]:
    return [f for f in files if BUILD_FILE.search(f)]


def compose_files(files: list[str]) -> list[str]:
    return [f for f in files
            if (COMPOSE_FILE.search(f) and not f.startswith(".github/")) or f in COMPOSE_FRAGMENTS]


def tracked(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p)


def split_reference(ref: str) -> tuple[str, str | None, str | None]:
    """``name``, ``tag`` and ``digest`` of an image reference as written."""
    name, _, digest = ref.partition("@")
    tag = None
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        name, tag = name[:colon], name[colon + 1 :]
    return name, tag, digest or None


def qualified(name: str) -> bool:
    first = name.split("/", 1)[0]
    return "/" in name and ("." in first or ":" in first or first == "localhost")


def reference_problems(ref: str) -> list[str]:
    if "$" in ref:
        return [f"{ref} is named by a variable this file resolves at run time; pin it here"]
    name, tag, digest = split_reference(ref)
    problems = []
    if not qualified(name):
        problems.append(f"{ref} is not fully qualified: write its registry, e.g. docker.io/library/…")
    if not tag or tag == "latest":
        problems.append(f"{ref} has {'the tag latest' if tag else 'no tag'}: pin a tag a refresh follows")
    if not digest or not DIGEST.fullmatch(digest):
        problems.append(f"{ref} has no sha256 digest: refresh_image_digests.py --write pins it")
    return problems


def logical_lines(text: str) -> list[tuple[int, str]]:
    """``(first line number, text)`` with ``\\``-continued lines joined."""
    out, buf, start = [], "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = number
        if raw.rstrip().endswith("\\"):
            buf += raw.rstrip()[:-1] + " "
            continue
        out.append((start, buf + raw))
        buf = ""
    if buf:
        out.append((start, buf))
    return out


def strip_comment(line: str) -> str:
    """A YAML or shell line without its comment: a ``#`` at the start or
    after whitespace, outside quotes."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1].isspace()):
            return line[:i]
    return line


def unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def dockerfile_references(path: str, text: str) -> tuple[list[Reference], int]:
    refs, aliases, froms = [], set(), 0
    for number, line in logical_lines(text):
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"\s*FROM\s+(.*)$", line, re.IGNORECASE)
        if not m:
            continue
        froms += 1
        words = [w for w in m.group(1).split() if not w.startswith("--")]
        if not words:
            refs.append(Reference(path, number, "", "FROM"))
            continue
        image = words[0]
        if len(words) >= 3 and words[1].lower() == "as":
            alias = words[2].lower()
        else:
            alias = None
        if image.lower() not in aliases and image != "scratch":
            refs.append(Reference(path, number, image, "FROM"))
        if alias:
            aliases.add(alias)
    return refs, froms


def compose_references(path: str, text: str) -> list[Reference]:
    refs = []
    for number, raw in enumerate(text.splitlines(), 1):
        m = re.match(r"\s*(?:-\s+)?image\s*:\s*(\S.*)$", strip_comment(raw))
        if not m:
            continue
        value = unquote(m.group(1))
        if value.startswith(FIRST_PARTY_COMPOSE):
            continue
        refs.append(Reference(path, number, value, "compose image:"))
    return refs


def words_of(command: str) -> list[tuple[str, bool]]:
    """Shell words, each with whether it was quoted whole. A ``${{ … }}``,
    ``${…}`` or ``$(…)`` stays inside its word; the control operators are
    words of their own, and so is a ``{`` or ``}`` standing alone."""
    out, i, n = [], 0, len(command)
    while i < n:
        ch = command[i]
        if ch.isspace():
            i += 1
            continue
        if command.startswith(("&&", "||"), i):
            out.append((command[i : i + 2], False))
            i += 2
            continue
        if ch in ";|&()":
            out.append((ch, False))
            i += 1
            continue
        word, quoted = "", None
        while i < n and not command[i].isspace() and command[i] not in ";|&()":
            c = command[i]
            if c in "'\"":
                j = command.find(c, i + 1)
                j = n if j < 0 else j
                word += command[i + 1 : j]
                quoted = True if quoted is None else quoted
                i = j + 1
                continue
            quoted = False
            if command.startswith("${{", i):
                j = command.find("}}", i)
                j = n if j < 0 else j + 2
            elif command.startswith("${", i):
                j = command.find("}", i)
                j = n if j < 0 else j + 1
            elif command.startswith("$(", i):
                depth, j = 0, i + 1
                while j < n:
                    depth += {"(": 1, ")": -1}.get(command[j], 0)
                    if depth == 0:
                        break
                    j += 1
                j += 1
            else:
                j = i + 1
            word += command[i:j]
            i = j
        out.append((word, bool(quoted)))
    return out


def engine_images(words: list[tuple[str, bool]]) -> list[tuple[str, str]]:
    """``(verb, image word)`` for each engine ``run``/``create``/``pull``."""
    found = []
    for k, (word, _) in enumerate(words):
        if word.rsplit("/", 1)[-1] not in ("docker", "podman"):
            continue
        if k and words[k - 1][0] in ("echo", "printf"):
            continue
        j = k + 1
        while j < len(words) and words[j][0].startswith("-"):
            opt = words[j][0]
            j += 2 if opt in GLOBAL_VALUE_OPTIONS and "=" not in opt else 1
        if j < len(words) and words[j][0] in ("container", "image"):
            j += 1
        if j >= len(words) or words[j][0] not in ("run", "create", "pull"):
            continue
        verb = words[j][0]
        values = PULL_VALUE_OPTIONS if verb == "pull" else RUN_VALUE_OPTIONS
        j += 1
        while j < len(words) and words[j][0] not in SEPARATORS:
            word, quoted = words[j]
            if word.startswith("-") and word != "-":
                name = word.split("=", 1)[0]
                j += 2 if name in values and "=" not in word else 1
                continue
            # An unquoted variable that is not an image and is followed by
            # an option is a list of options (``$otel``), not the image.
            if (word.startswith("$") and not quoted and not SHELL_IMAGE.fullmatch(word)
                    and j + 1 < len(words) and words[j + 1][0].startswith("-")):
                j += 1
                continue
            found.append((verb, word))
            break
        else:
            found.append((verb, ""))
    return found


def workflow_references(path: str, text: str) -> tuple[list[Reference], list[str]]:
    refs, problems = [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = strip_comment(raw)
        m = re.match(r"\s*(?:-\s+)?image\s*:\s*(\S.*)$", line)
        if m:
            value = unquote(m.group(1))
            if LOCAL_MATRIX_IMAGE.fullmatch(value) or EXPRESSION_IMAGE.fullmatch(value):
                continue
            refs.append(Reference(path, number, value, "workflow image:"))
            continue
        m = re.match(r"\s*(?:-\s+)?(\w*_IMAGE)\s*:\s*(\S.*)$", line)
        if m:
            refs.append(Reference(path, number, unquote(m.group(2)), f"workflow {m.group(1)}:"))
    for number, line in logical_lines(text):
        command = strip_comment(line)
        for verb, image in engine_images(words_of(command)):
            if SHELL_IMAGE.fullmatch(image) or EXPRESSION_IMAGE.fullmatch(image):
                continue
            problems.append(
                f"{path}:{number}: images — a workflow `{verb}` names its image "
                f"{image or '(none found)'!s} by a literal: name it by an …_IMAGE variable "
                "whose value is pinned"
            )
    return refs, problems


def matrix_rows(text: str) -> list[str]:
    return [
        line for line in text.splitlines()
        if line.startswith("|") and not set(line.strip()) <= set("|-: ")
    ]


def matrix_components(rows: list[str]) -> set[str]:
    """Each code span in a row's first cell, its Component, whole. Compared
    exactly, so ``postgres:16`` is not found inside ``postgres:16-alpine``'s
    row, and a lock's path not inside a longer one."""
    spans: set[str] = set()
    for row in rows:
        first = row.strip().strip("|").split("|")[0]
        spans.update(span.strip() for span in re.findall(r"`([^`]+)`", first))
    return spans


def third_party_licence(root: Path, package: str) -> str | None:
    text = (root / "THIRD_PARTY.md").read_text(encoding="utf-8")
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) >= 3 and cells[0].lower() == package:
            return cells[2]
    return None


def lock_version(root: Path, package: str) -> str | None:
    path = root / GATEWAY_LOCK
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(rf"{package}==([^\s;\\]+)", line.strip(), re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rules", nargs="*", help="images, matrix (default: both)")
    parser.add_argument("--root", default=".", help="the checkout to read (default: here)")
    args = parser.parse_args(argv)
    unknown = set(args.rules) - {"images", "matrix"}
    if unknown:
        parser.error(f"no such rule: {', '.join(sorted(unknown))}")
    rules = set(args.rules) or {"images", "matrix"}
    root = Path(args.root).resolve()
    files = tracked(root)
    dockerfiles = build_files(files)
    composes = compose_files(files)
    workflows = [f for f in files if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", f)]
    locks = [f for f in files if LOCK_NAMES.fullmatch(Path(f).name)]
    if not dockerfiles or not composes or not workflows:
        print(
            f"::error::dependency-identity read {len(dockerfiles)} Dockerfile(s), "
            f"{len(composes)} compose file(s) and {len(workflows)} workflow(s) under {root}. "
            "A guard that read nothing has proved nothing."
        )
        return 1

    refs: list[Reference] = []
    findings: list[str] = []
    froms = 0
    for f in dockerfiles:
        found, n = dockerfile_references(f, (root / f).read_text(encoding="utf-8"))
        refs += found
        froms += n
    for f in composes:
        refs += compose_references(f, (root / f).read_text(encoding="utf-8"))
    for f in workflows:
        found, problems = workflow_references(f, (root / f).read_text(encoding="utf-8"))
        refs += found
        if "images" in rules:
            findings += problems
    if not refs or not froms:
        print("::error::dependency-identity found no image reference: it read nothing it can vouch for.")
        return 1

    if "images" in rules:
        for f in COMPOSE_FRAGMENTS:
            if f not in files:
                findings.append(
                    f"{f}: images — the compose fragment `librerun init` renders is not "
                    "tracked here: point COMPOSE_FRAGMENTS at where it moved"
                )
        for r in refs:
            if not r.text:
                findings.append(f"{r.path}:{r.line}: images — a {r.what} with no image")
            for p in reference_problems(r.text) if r.text else []:
                findings.append(f"{r.path}:{r.line}: images — {r.what} {p}")

    litellm = licence = None
    if "matrix" in rules:
        matrix = root / MATRIX
        if not matrix.is_file():
            findings.append(f"{MATRIX}: matrix — the distribution surface matrix is missing")
        else:
            rows = matrix_rows(matrix.read_text(encoding="utf-8"))
            components = matrix_components(rows)
            if not rows:
                findings.append(f"{MATRIX}: matrix — the matrix has no table row")
            for r in refs:
                if not r.text or "$" in r.text:
                    continue
                name, tag, _ = split_reference(r.text)
                entry = f"{name}:{tag}" if tag else name
                if entry not in components:
                    findings.append(f"{r.path}:{r.line}: matrix — {entry} is in no row of {MATRIX}")
            for lock in locks:
                if lock not in components:
                    findings.append(f"{lock}: matrix — the lock is in no row of {MATRIX}")
            litellm = lock_version(root, "litellm")
            licence = third_party_licence(root, "litellm")
            if not litellm:
                findings.append(f"{GATEWAY_LOCK}: matrix — no `litellm==` pin to hold the matrix to")
            if not licence:
                findings.append("THIRD_PARTY.md: matrix — no litellm row to read its licence from")
            if litellm and licence:
                lite_rows = [row for row in rows
                             if "litellm" in row.strip().strip("|").split("|")[0].lower()]
                if not any(re.search(rf"(?<![\w.]){re.escape(litellm)}(?![\w.])", row)
                           and licence in row for row in lite_rows):
                    findings.append(
                        f"{MATRIX}: matrix — no LiteLLM row holds the lock's version {litellm} "
                        f"and THIRD_PARTY.md's licence {licence}"
                    )

    if findings:
        for line in sorted(set(findings)):
            print(line)
        print(
            f"::error::dependency-identity: {len(set(findings))} finding(s) above. Every image is "
            "pinned by tag and digest and moves only by a reviewed refresh (R14, D25), and the "
            "distribution surface matrix accounts for each one (D27)."
        )
        return 1
    print(
        f"clean: {len(refs)} image reference(s) read — {froms} FROM line(s) in "
        f"{len(dockerfiles)} Dockerfile(s), {len(composes)} compose file(s), "
        f"{len(workflows)} workflow(s) — and {len(locks)} lock(s)"
        + (f"; LiteLLM {litellm} ({licence}) is the matrix's LiteLLM row" if litellm else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
