#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""The licensing checks (docs/release/License_Scope_Map.md).

Every expectation here is DERIVED from REUSE.toml, the licence scope map
in machine-readable form, so the map, the headers, the package metadata,
the image labels and NOTICE cannot drift apart without this failing.
Standard library only (Python 3.11+, for tomllib).

    python3 scripts/check_licensing.py                # every rule
    python3 scripts/check_licensing.py headers notice # some rules

Rules:
    headers      every standalone script, and every file that carries an
                 SPDX licence header, says what REUSE.toml says for it
    metadata     every package manifest and image label names exactly the
                 licences of the files that package or image carries
    notice       LICENSE is the FSF's AGPL-3.0 text; NOTICE carries the
                 qualified copyright statement and the separate grant,
                 the carve-outs and every third-party notice
    statements   no current document says the project is Apache-2.0, or
                 that a company holds its copyright or its name
    bundle       a source archive, as `git archive` itself lists it, carries
                 LICENSE, NOTICE, TRADEMARKS.md, REUSE.toml, THIRD_PARTY.md
                 and every licence text used
    third-party  every locked or declared dependency and every image is in
                 THIRD_PARTY.md with a licence, and no first-party file
                 carries a copyright notice REUSE.toml does not account for

Exit status 1 names every violation; 0 means clean. Each rule is
negative-tested by scripts/licensing_probes.sh, which injects the
violation the rule exists to catch and fails if the rule stays green.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True,
                           text=True, check=True).stdout.strip())

# The FSF's AGPL-3.0 text, byte for byte (https://www.gnu.org/licenses/agpl-3.0.txt).
AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"

# JR, 2026-09-23: two statements, deliberately separate, verbatim.
QUALIFIED_COPYRIGHT = (
    "Copyright © 2026 Jeremiah Ross, to the extent copyright subsists in first-party "
    "material and such copyright is owned by Jeremiah Ross. No copyright is claimed in "
    "AI-generated material that is not eligible for copyright protection under "
    "applicable law."
)
LICENSING_GRANT = (
    "To the extent copyright subsists, copyrightable first-party material owned by "
    "Jeremiah Ross is licensed under AGPL-3.0-only."
)
# REUSE.toml's own first-party lines; every OTHER copyright line there is a
# third-party notice, and NOTICE must carry it.
FIRST_PARTY_LINES = ("2026 Jeremiah Ross, to the extent", "LibreRun contributors, each in")

# What each image built from this tree carries (its COPY sources). A
# Dockerfile that declares a licences label must be listed here.
IMAGES = {
    "backend/Dockerfile": ["backend/"],
    "frontend/Dockerfile": ["frontend/"],
    "services/gateway/Dockerfile": ["backend/app/", "services/gateway/"],
    "backend/agents/_examples/echo_container/Dockerfile": [
        "sdk/python/librerun-agent/", "backend/agents/_examples/echo_container/"],
    "backend/agents/_examples/llamaindex_summarize/Dockerfile": [
        "sdk/python/librerun-agent/", "backend/agents/_examples/llamaindex_summarize/"],
    "backend/agents/_examples/vercel_ai_answer_ts/Dockerfile": [
        "backend/agents/_examples/vercel_ai_answer_ts/"],
}
LABEL = re.compile(r'org\.opencontainers\.image\.licenses="([^"]*)"')

# First-party packages: never third-party rows.
FIRST_PARTY_PACKAGES = {"librerun", "librerun-agent", "librerun-langgraph", "vita-agent"}

# Records that must stay as they were written: released changelog
# sections, the licence scope map, and this checker and its probes, which
# quote what they forbid.
HISTORICAL = (
    "CHANGELOG.md",
    "docs/release/License_Scope_Map.md",
    "scripts/check_licensing.py",
    "scripts/licensing_probes.sh",
    ".github/workflows/release-readiness.yml",
    "LICENSES/",
    "LICENSE",
    "THIRD_PARTY.md",
)

# Statements that were true of the tree before the AGPL, or never were.
STALE = [
    (r"(?:released|published|licensed|distributed)\s+under\s+the\s+\W*Apache", "says the project is under Apache-2.0; it is AGPL-3.0-only, with four Apache-2.0 directories"),
    (r"project's\s+Apache-2\.0\s+licen[cs]e", "says the project's licence is Apache-2.0"),
    (r"licen[cs]e\s+the\s+whole\s+repository\s+carries|L17\s+is\s+the\s+whole\s+repository", "says one licence covers the whole repository (L17); L36 replaced it"),
    (r"copyright\s+(?:entity|holder)\s+is\s+the\s+company|company's\s+exact\s+legal\s+name", "names a company as the copyright holder (L36: Jeremiah Ross, qualified)"),
    (r"marks\s+of\s+the\s+copyright\s+holder", "ties the name to the copyright holder; TRADEMARKS.md governs the name separately"),
    (r"Apache\s+License\s+2\.0\s+section\s+6", "rests the name on Apache-2.0 section 6"),
    # #148 item 5: the two wordings #147 removed, which nothing above
    # caught. The first opens a sentence (a capital L), because NOTICE's
    # own carve-out sentence says "are licensed under Apache-2.0" and is
    # right to; the second is the whole-tree claim L36 replaced.
    (r"(?<![\w-])(?-i:Licensed)\s+under\s+(?:the\s+)?\W*Apache", "opens a sentence saying it is licensed under Apache-2.0; the project is AGPL-3.0-only, with four Apache-2.0 directories"),
    (r"Apache(?:[-\s]2\.0|\s+Licen[cs]e(?:,?\s+Version)?\s+2\.0)?\W{0,4}across\s+the\s+whole\s+(?:tree|repository)", "says Apache-2.0 covers the whole tree or repository (L17); L36 replaced it"),
]
MUST_SAY_AGPL = ["README.md", "CONTRIBUTING.md", "TRADEMARKS.md", "NOTICE",
                 ".github/pull_request_template.md"]

FOREIGN_COPYRIGHT = re.compile(r"(?i)\bcopyright\b\s*(?:\(c\)|©)?\s*(?:19|20)\d\d")

failures: list[str] = []


def fail(rule: str, msg: str) -> None:
    failures.append(f"[{rule}] {msg}")


def tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return sorted(p for p in out.decode().split("\0") if p and (ROOT / p).exists())


def read_text(path: str) -> str | None:
    p = ROOT / path
    if p.is_symlink() or not p.is_file():
        return None
    data = p.read_bytes()
    if b"\0" in data[:8000]:
        return None
    return data.decode("utf-8", "replace")


def is_historical(path: str) -> bool:
    return any(path == h or (h.endswith("/") and path.startswith(h)) for h in HISTORICAL)


# ---------------------------------------------------------------- REUSE.toml
def glob_to_re(glob: str) -> re.Pattern:
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**", i):
            out += ".*"
            i += 2
        elif glob[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")


def load_annotations() -> list[dict]:
    data = tomllib.loads((ROOT / "REUSE.toml").read_text(encoding="utf-8"))
    anns = []
    for a in data.get("annotations", []):
        paths = a["path"] if isinstance(a["path"], list) else [a["path"]]
        cr = a.get("SPDX-FileCopyrightText", [])
        anns.append({
            "paths": paths,
            "res": [glob_to_re(p) for p in paths],
            "licence": a["SPDX-License-Identifier"],
            "copyright": cr if isinstance(cr, list) else [cr],
        })
    return anns


ANNOTATIONS = load_annotations()


def annotation_for(path: str) -> dict | None:
    hit = None
    for a in ANNOTATIONS:          # REUSE: the LAST matching annotation wins
        if any(r.match(path) for r in a["res"]):
            hit = a
    return hit


def licence_ids(expr: str) -> set[str]:
    ids = set(re.findall(r"[A-Za-z0-9.+\-]+", expr)) - {"AND", "OR", "WITH"}
    return ids


def conjunction(expr: str) -> set[str]:
    if re.search(r"\bOR\b|\bWITH\b", expr):
        raise ValueError(expr)
    return licence_ids(expr)


def header_licence(text: str) -> str | None:
    for line in text.splitlines()[:5]:
        m = re.search(r"SPDX-License-Identifier:\s*(.+?)\s*(?:\*/|-->)?\s*$", line)
        if m:
            return m.group(1)
    return None


# ---------------------------------------------------------------- rules
def rule_headers(files: list[str]) -> None:
    scripts = [f for f in files if f.endswith(".sh") or re.fullmatch(r"scripts/[^/]+\.py", f)]
    for f in files:
        text = read_text(f)
        if text is None:
            continue
        declared = header_licence(text)
        if f in scripts and declared is None:
            fail("headers", f"{f}: a standalone script with no SPDX-License-Identifier in its first five lines "
                            "(it travels without LICENSE beside it; add the header REUSE.toml's licence for it names)")
            continue
        if declared is None:
            continue
        ann = annotation_for(f)
        if ann is None:
            fail("headers", f"{f}: carries a licence header, but REUSE.toml does not cover the path")
        elif set(licence_ids(declared)) != set(licence_ids(ann["licence"])):
            fail("headers", f"{f}: its header says {declared!r}; REUSE.toml says {ann['licence']!r}")


def licences_under(files: list[str], prefixes: list[str]) -> set[str]:
    ids: set[str] = set()
    for f in files:
        if any(f.startswith(p) for p in prefixes):
            ann = annotation_for(f)
            if ann is None:
                fail("metadata", f"{f}: not covered by REUSE.toml")
                continue
            ids |= conjunction(ann["licence"])
    return ids


def rule_metadata(files: list[str]) -> None:
    for f in files:
        name = Path(f).name
        if name not in ("pyproject.toml", "package.json"):
            continue
        base = str(Path(f).parent) + "/"
        if name == "pyproject.toml":
            proj = tomllib.loads(read_text(f)).get("project", {})
            lic = proj.get("license")
            declared = lic.get("text") if isinstance(lic, dict) else lic
        else:
            declared = json.loads(read_text(f)).get("license")
        if not declared:
            fail("metadata", f"{f}: declares no licence")
            continue
        want = licences_under(files, [base])
        try:
            got = conjunction(declared)
        except ValueError:
            fail("metadata", f"{f}: declares {declared!r}; a first-party package states an AND of its licences, never a choice")
            continue
        if got != want:
            fail("metadata", f"{f}: declares {declared!r}; its files carry {' AND '.join(sorted(want))}")

    labelled = {f for f in files if Path(f).name.startswith("Dockerfile") and LABEL.search(read_text(f) or "")}
    for f in sorted(labelled - set(IMAGES)):
        fail("metadata", f"{f}: declares a licences label but is not in IMAGES in scripts/check_licensing.py "
                         "(say what the image carries, so the label can be checked)")
    for dockerfile, prefixes in IMAGES.items():
        m = LABEL.search(read_text(dockerfile) or "")
        if not m:
            fail("metadata", f"{dockerfile}: no org.opencontainers.image.licenses label")
            continue
        want = licences_under(files, prefixes)
        got = conjunction(m.group(1))
        if got != want:
            fail("metadata", f"{dockerfile}: its label says {m.group(1)!r}; the image carries {' AND '.join(sorted(want))}")


def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def rule_notice(files: list[str]) -> None:
    lic = (ROOT / "LICENSE").read_bytes() if (ROOT / "LICENSE").is_file() else b""
    if hashlib.sha256(lic).hexdigest() != AGPL_SHA256:
        fail("notice", "LICENSE is not the FSF's AGPL-3.0 text byte for byte (sha256 "
                       f"{hashlib.sha256(lic).hexdigest()[:16]}…, want {AGPL_SHA256[:16]}…)")
    copy = ROOT / "LICENSES" / "AGPL-3.0-only.txt"
    if not copy.is_file() or copy.read_bytes() != lic:
        fail("notice", "LICENSES/AGPL-3.0-only.txt is not byte-identical to LICENSE")

    used = set()
    for a in ANNOTATIONS:
        used |= licence_ids(a["licence"])
    for lid in sorted(used):
        if not (ROOT / "LICENSES" / f"{lid}.txt").is_file():
            fail("notice", f"REUSE.toml uses {lid} but LICENSES/{lid}.txt is missing")

    notice = squash(read_text("NOTICE") or "")
    for label, text in (("the qualified copyright statement", QUALIFIED_COPYRIGHT),
                        ("the licensing grant", LICENSING_GRANT)):
        if squash(text) not in notice:
            fail("notice", f"NOTICE does not carry {label} verbatim")
    for a in ANNOTATIONS:
        if a["licence"] == "Apache-2.0":
            for p in a["paths"]:
                d = p[:-2] if p.endswith("/**") else p
                if d not in notice:
                    fail("notice", f"NOTICE does not list the Apache-2.0 carve-out {d}")
        for line in a["copyright"]:
            if line.startswith(FIRST_PARTY_LINES):
                continue
            holder = squash(re.sub(r"\s*\([^)]*\)\s*$", "", line))
            if holder not in notice:
                fail("notice", f"NOTICE does not carry the third-party notice {holder!r} that REUSE.toml records")
    for lid in sorted(used):
        if f"LICENSES/{lid}.txt" not in notice:
            fail("notice", f"NOTICE does not point at LICENSES/{lid}.txt")


def rule_statements(files: list[str]) -> None:
    for f in files:
        if is_historical(f):
            continue
        text = read_text(f)
        if text is None:
            continue
        flat = squash(text)
        for pattern, why in STALE:
            m = re.search(pattern, flat, re.I)
            if m:
                fail("statements", f"{f}: {why} ({m.group(0)!r})")
    for f in MUST_SAY_AGPL:
        if "AGPL-3.0-only" not in (read_text(f) or ""):
            fail("statements", f"{f}: does not name AGPL-3.0-only")


def rule_bundle(files: list[str]) -> None:
    used = set()
    for a in ANNOTATIONS:
        used |= licence_ids(a["licence"])
    need = ["LICENSE", "NOTICE", "TRADEMARKS.md", "REUSE.toml", "THIRD_PARTY.md"] + \
           [f"LICENSES/{lid}.txt" for lid in sorted(used)]
    have = set(files)
    for f in need:
        if f not in have:
            fail("bundle", f"{f} is not tracked, so a source archive would not carry it")
    # What a source archive carries is `git archive`'s own listing, not a
    # reading of each path's attribute (#148 item 2): an export-ignore on a
    # directory drops everything under it, and `LICENSES/ export-ignore`
    # leaves `git check-attr` reporting "unspecified" for the directory and
    # for every file in it. HEAD's tree with the working tree's
    # .gitattributes, so an export-ignore not yet committed counts too; a
    # needed file not yet committed is the tracked check's, above.
    committed = subprocess.run(["git", "ls-tree", "-r", "-z", "--name-only", "HEAD", "--"] + need,
                               cwd=ROOT, capture_output=True, check=True).stdout.decode().split("\0")
    wanted = [f for f in need if f in committed]
    if not wanted:
        return
    archive = subprocess.run(["git", "archive", "--worktree-attributes", "--format=tar", "HEAD", "--"] + wanted,
                             cwd=ROOT, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        carried = set(tar.getnames())
    for f in wanted:
        if f not in carried:
            fail("bundle", f"{f} is left out of `git archive` (export-ignore on it or on a directory above it), "
                           "so a source archive would not carry it")


def pep503(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def third_party_rows() -> tuple[dict[str, set[str]], set[str], list[str]]:
    packages: dict[str, set[str]] = {}
    images: set[str] = set()
    problems = []
    header = None
    for line in (read_text("THIRD_PARTY.md") or "").splitlines():
        if not line.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if header is None:
            header = cells
            continue
        if set(line) <= set("|- "):
            continue
        row = dict(zip(header, cells))
        licence = row.get("Licence", "")
        if header[:2] == ["Package", "Version"]:
            packages.setdefault(pep503(row["Package"]), set()).add(row["Version"])
        elif header[:2] == ["Image", "Tag"]:
            # A row that records a digest keys on it (A3, R14), so the exact
            # match in rule_third_party binds the reference's digest too: a
            # digest moved in compose or a Dockerfile, and not here, fails.
            digest = row.get("Digest", "")
            images.add(f"{row['Image']}:{row['Tag']}" + (f"@{digest}" if digest else ""))
        else:
            continue
        if not licence or licence.upper() in ("UNKNOWN", "NONE", "?"):
            problems.append(f"THIRD_PARTY.md: {cells[0]} {cells[1]} has no licence")
    return packages, images, problems


def requirement_names(text: str) -> list[tuple[str, str | None]]:
    out = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "\\")):
            continue
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*(==\s*([^\s;\\]+))?", line)
        if m:
            out.append((m.group(1), m.group(3)))
    return out


def rule_third_party(files: list[str]) -> None:
    packages, images, problems = third_party_rows()
    for p in problems:
        fail("third-party", p)

    def need(name: str, version: str | None, where: str) -> None:
        key = pep503(name)
        if key in FIRST_PARTY_PACKAGES:
            return
        if key not in packages:
            fail("third-party", f"{where}: {name} has no row in THIRD_PARTY.md")
        elif version and version not in packages[key]:
            fail("third-party", f"{where}: {name} {version} has no row in THIRD_PARTY.md "
                                f"(rows: {', '.join(sorted(packages[key]))})")

    for f in files:
        name = Path(f).name
        if re.fullmatch(r"requirements(\.lock)?\.txt", name):
            pinned = name == "requirements.lock.txt"
            for n, v in requirement_names(read_text(f) or ""):
                need(n, v if pinned else None, f)
        elif name == "pyproject.toml":
            proj = tomllib.loads(read_text(f)).get("project", {})
            deps = list(proj.get("dependencies", []))
            for extra in proj.get("optional-dependencies", {}).values():
                deps += extra
            for n, _ in requirement_names("\n".join(deps)):
                need(n, None, f)
        elif name == "package-lock.json":
            lock = json.loads(read_text(f))
            for path, meta in lock.get("packages", {}).items():
                if not path:
                    continue
                pkg = meta.get("name") or path.rsplit("node_modules/", 1)[-1]
                if meta.get("link"):
                    continue
                need(pkg, meta.get("version"), f)
        elif name == "package.json":
            pj = json.loads(read_text(f))
            for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
                for n in pj.get(section, {}):
                    need(n, None, f)

    refs = []
    for f in files:
        text = read_text(f) or ""
        if Path(f).name.startswith("Dockerfile"):
            stages = set(re.findall(r"(?im)^FROM\s+\S+\s+AS\s+(\S+)", text))
            for m in re.finditer(r"(?im)^FROM\s+(?:--\S+\s+)*(\S+)", text):
                if m.group(1) not in stages:
                    refs.append((f, m.group(1)))
        elif f in ("compose.yaml", "agents.compose.yaml"):
            for m in re.finditer(r"(?m)^\s+image:\s*(\S+)", text):
                if "${" not in m.group(1):
                    refs.append((f, m.group(1)))
    for f, ref in refs:
        if ref not in images:
            fail("third-party", f"{f}: the image {ref} has no row in THIRD_PARTY.md")

    third_party_paths = set()
    for a in ANNOTATIONS:
        if any(not c.startswith(FIRST_PARTY_LINES) for c in a["copyright"]):
            third_party_paths |= set(a["paths"])
    for f in files:
        if is_historical(f) or f in ("NOTICE", "REUSE.toml") or f.endswith(("package-lock.json", ".lock.txt")):
            continue
        ann = annotation_for(f)
        if ann and any(not c.startswith(FIRST_PARTY_LINES) for c in ann["copyright"]):
            continue
        text = read_text(f)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if FOREIGN_COPYRIGHT.search(line) and "Jeremiah Ross" not in line:
                fail("third-party", f"{f}:{n}: a copyright notice in a file REUSE.toml attributes to the project "
                                    "— if the text is third-party, annotate it in REUSE.toml and NOTICE")


RULES = {
    "headers": rule_headers,
    "metadata": rule_metadata,
    "notice": rule_notice,
    "statements": rule_statements,
    "bundle": rule_bundle,
    "third-party": rule_third_party,
}


def main(argv: list[str]) -> int:
    chosen = argv or list(RULES)
    unknown = [r for r in chosen if r not in RULES]
    if unknown:
        print(f"unknown rule(s): {', '.join(unknown)}; the rules are {', '.join(RULES)}", file=sys.stderr)
        return 2
    files = tracked()
    for r in chosen:
        RULES[r](files)
    if failures:
        for f in failures:
            print(f"::error::{f}")
        print(f"{len(failures)} licensing violation(s); docs/release/License_Scope_Map.md says what each rule holds.")
        return 1
    print(f"clean: {', '.join(chosen)} ({len(files)} tracked files)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
