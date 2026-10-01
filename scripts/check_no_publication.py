#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""R11 — no workflow in this tree can publish anything (L37, D26).

A LibreRun release is source only: a tag makes a GitHub Release with notes
and the source archives GitHub attaches to every release, and nothing else —
no image, no wheel, no package, no asset (docs/platform/Releasing.md §3).
Deleting the jobs that published is not enough on its own: a step behind
``if: false`` is one edit from running, and a token that may write packages
is a capability whether or not a step uses it. So this reads every workflow
(``.github/workflows/*.y*ml``) and every local action
(``.github/actions/**/action.y*ml``), comments stripped and ``if:`` ignored,
and exits 1 naming file, line and rule for:

* a permission that could publish or sign: ``write-all``, ``packages:
  write``, ``id-token: write``, ``attestations: write``, and ``pages:
  write`` anywhere but ``docs-site.yml`` — the documentation site D21
  allows by name, which alone may also hold the ``id-token: write`` that
  ``actions/deploy-pages`` needs;
* a workflow with no top-level ``permissions:``, whose token would take the
  repository's default, which is not in the tree;
* a registry login: an action whose name says ``login``
  (``docker/login-action``, ``redhat-actions/podman-login`` and the like),
  or ``login`` by an engine or registry client — ``docker``, ``podman``,
  ``buildah``, ``nerdctl``, ``ctr``, ``skopeo``, ``oras``, ``crane`` or
  ``regctl``;
* a push or a cache export: ``push:`` with any value but ``false``,
  ``--push``, ``cache-to:`` or ``--cache-to``; ``push`` by one of those
  engines, by a compose (``docker-compose``, ``podman-compose``,
  ``compose.sh``) or by ``git``; a registry exporter, which pushes with no
  ``push`` in sight — ``type=registry``, or ``push=true`` in an exporter's
  spec, wherever it is written (``--output``, ``-o``, an action's
  ``outputs:``, ``--set``, the environment), a ``cache-from`` on the same
  line aside, since that one reads; ``buildx imagetools create``; and a
  registry client's writes (``skopeo`` copy, sync or delete; ``crane``
  copy, cp, append, mutate, edit, tag, rebase, flatten, index or delete;
  ``regctl`` copy, put, mod, add, append, create or delete; ``oras`` cp,
  copy, attach, tag or delete);
* a package publish: ``pypa/gh-action-pypi-publish``,
  ``JS-DevTools/npm-publish``, ``twine upload``, and ``publish`` by
  ``npm``, ``pnpm``, ``yarn``, ``bun``, ``uv``, ``poetry``, ``hatch``,
  ``flit``, ``pdm`` or ``cargo``;
* a release asset: ``gh release upload``; a word after ``gh release
  create``'s tag that is neither a flag nor a flag's value — the command's
  ``\\``-continued lines joined, its value-taking flags known, and a bare
  variable read as flags only when its name says so (``$flags``), since
  any other could expand to a file; a third-party release action (a
  ``uses:`` whose name says ``release``);
* a secret: any use of the ``secrets`` context in a ``${{ … }}``
  expression but ``secrets.GITHUB_TOKEN`` — a name, a bracketed or
  computed one, or the whole context (``toJSON(secrets)``) — or
  ``secrets: inherit``. The text ``secrets.`` alone is no expression — it
  occurs twenty-two times in ``librerun-smoke.yml``, as Python and file
  names.

A key is read however YAML lets it be written — plain, single- or
double-quoted, with spaces before its colon, in a block or a flow mapping
— and so is a permission's value: ``"packages": 'write'`` is
``packages: write``.

It fails having read nothing, and names what it read when it passes.

    python3 scripts/check_no_publication.py              # this tree
    python3 scripts/check_no_publication.py --root DIR   # another checkout
"""
from __future__ import annotations

import argparse
import re
import shlex
import sys
from pathlib import Path

DOCS_SITE = "docs-site.yml"

ENGINES = frozenset(
    {"docker", "podman", "buildah", "nerdctl", "ctr", "skopeo", "oras", "crane", "regctl"}
)
# What can push beside the engines: the composes (``docker compose push``
# is docker's) and git.
PUSHERS = ENGINES | frozenset({"docker-compose", "podman-compose", "compose.sh", "git"})
PUBLISHERS = frozenset(
    {"npm", "pnpm", "yarn", "bun", "uv", "poetry", "hatch", "flit", "pdm", "cargo"}
)
# The clients whose work is moving images between registries, and the
# verbs by which each writes to one without saying ``push``.
REGISTRY_WRITES = {
    "skopeo": frozenset({"copy", "sync", "delete"}),
    "crane": frozenset(
        {"copy", "cp", "append", "mutate", "edit", "tag", "rebase", "flatten", "index", "delete"}
    ),
    "regctl": frozenset({"copy", "put", "mod", "add", "append", "create", "delete"}),
    "oras": frozenset({"cp", "copy", "attach", "tag", "delete"}),
}
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "{", "}"})
# ``gh release create``'s flags that take the next word as their value.
CREATE_VALUE_FLAGS = frozenset(
    {"--title", "-t", "--notes", "-n", "--notes-file", "-F", "--target", "--discussion-category"}
)


def key(name: str) -> str:
    """A YAML mapping key ``name`` however YAML lets it be written — plain,
    single- or double-quoted, spaces before the colon, in a block or a flow
    mapping — up to and including its colon."""
    name = re.escape(name)
    return rf"""(?<![\w-])(?:{name}|'{name}'|"{name}")\s*:"""


def scalar(value: str) -> str:
    """A YAML scalar ``value``, plain or quoted, and nothing longer."""
    value = re.escape(value)
    return rf"""\s*(?:{value}|'{value}'|"{value}")(?![\w-])"""


def permission(scope: str) -> re.Pattern[str]:
    return re.compile(key(scope) + scalar("write"), re.IGNORECASE)


PERMISSION_RULES = (
    (re.compile(r"(?<![\w-])write-all(?![\w-])", re.IGNORECASE), "permissions: write-all"),
    (permission("packages"), "packages: write"),
    (permission("attestations"), "attestations: write"),
)
ID_TOKEN = permission("id-token")
PAGES = permission("pages")
USES = re.compile(key("uses") + r"""\s*['"]?(?P<action>[^\s'"#,}\]]+)""")
PUBLISH_ACTIONS = ("pypa/gh-action-pypi-publish", "js-devtools/npm-publish")
# ``push:``'s value runs to the end of the line, or of its flow mapping's
# entry; an empty one is the ``on: push:`` trigger's, a mapping or a list
# is a trigger's filter, and ``false`` is the one value that does not push.
PUSH_KEY = re.compile(key("push") + r"""\s*(?P<value>[^\s,}\]][^,}\]]*)?""")
CACHE_TO_KEY = re.compile(key("cache-to"))
# A registry exporter pushes with no ``push`` in sight: ``type=registry``,
# or ``push=true`` in an exporter's spec (buildx reads 1 and t as true).
EXPORTER = re.compile(
    r"""(?:^|(?<=[\s,"'=])|(?<=-o))(?P<spec>type=registry|push=(?:true|1|t))(?=$|[\s,"'])""",
    re.IGNORECASE,
)
EXPRESSION = re.compile(r"\$\{\{(?P<body>.*?)(?:\}\}|$)")
# The ``secrets`` context itself — not a property that happens to share the
# name (``inputs.secrets``) nor a longer word (``secrets_dir``).
SECRETS_CONTEXT = re.compile(r"(?<![\w.-])secrets(?![\w-])")
ONLY_THE_TOKEN = re.compile(r"""\s*(?:\.\s*GITHUB_TOKEN|\[\s*(['"])GITHUB_TOKEN\1\s*\])(?![\w-])""")
SECRET_NAME = re.compile(r"""\s*(?:\.\s*(?P<dot>[A-Za-z_][\w-]*)|\[\s*['"](?P<bracket>[^'"]+)['"]\s*\])""")
SECRETS_INHERIT = re.compile(key("secrets") + scalar("inherit"))
TOP_LEVEL_PERMISSIONS = re.compile(r"""^(?:permissions|'permissions'|"permissions")\s*:""")
VARIABLE = re.compile(r"^\$\{?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}?$")


def strip_comment(line: str) -> str:
    """``line`` without a ``#`` comment: YAML's and the shell's alike — a
    ``#`` at the start or after whitespace, outside quotes."""
    single = double = False
    previous = " "
    for index, char in enumerate(line):
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single and previous != "\\":
            double = not double
        elif char == "#" and not single and not double and previous in " \t":
            return line[:index].rstrip()
        previous = char
    return line.rstrip()


def tokens(text: str) -> list[str]:
    """Shell words and separators, quotes removed; a line the tokenizer
    cannot read is split on whitespace rather than skipped."""
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.wordchars += "$%{}:,@+"
    try:
        return list(lexer)
    except ValueError:
        return text.split()


def commands(words: list[str]) -> list[list[str]]:
    """``words`` cut into simple commands at the shell's separators."""
    out: list[list[str]] = [[]]
    for word in words:
        if word in SEPARATORS:
            out.append([])
        else:
            out[-1].append(word)
    return [command for command in out if command]


def verbs(command: list[str], start: int) -> list[str]:
    """Every word after ``command[start]`` that is not an option. All of
    them, not the first: an option's value sits before the verb in
    ``docker compose --profile app push`` and ``git -c k=v push``."""
    return [word for word in command[start + 1 :] if not word.startswith("-")]


def logical_lines(lines: list[str]) -> list[list[tuple[str, int]]]:
    """Each command line with its ``\\``-continued lines joined, as
    (word, line number) pairs, so a finding names the line its word is on."""
    out: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = []
    for number, line in enumerate(lines, start=1):
        text = line.rstrip()
        continued = text.endswith("\\")
        if continued:
            text = text[:-1]
        current.extend((word, number) for word in tokens(text))
        if not continued:
            if current:
                out.append(current)
            current = []
    if current:
        out.append(current)
    return out


def check_release_create(
    words: list[tuple[str, int]], path: str
) -> list[tuple[str, int, str, str]]:
    found = []
    plain = [word for word, _ in words]
    for index in range(len(plain) - 2):
        if plain[index : index + 3] != ["gh", "release", "create"]:
            continue
        position = index + 3
        tag_seen = False
        while position < len(plain) and plain[position] not in SEPARATORS:
            word, number = words[position]
            position += 1
            if word.startswith("-"):
                if word in CREATE_VALUE_FLAGS:
                    position += 1
                continue
            if not tag_seen:
                tag_seen = True
                continue
            variable = VARIABLE.match(word)
            if variable and "flag" in variable.group("name").lower():
                continue
            found.append((path, number, "release asset: a file after gh release create's tag", word))
    return found


def check_file(path: Path, root: Path, *, workflow: bool) -> list[tuple[str, int, str, str]]:
    shown = str(path.relative_to(root))
    raw = path.read_text(encoding="utf-8").splitlines()
    lines = [strip_comment(line) for line in raw]
    found: list[tuple[str, int, str, str]] = []
    docs_site = workflow and path.name == DOCS_SITE

    if workflow and not any(TOP_LEVEL_PERMISSIONS.match(line) for line in lines):
        found.append((shown, 1, "no top-level permissions: (the token would take the repository's default)", ""))

    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        for pattern, rule in PERMISSION_RULES:
            if pattern.search(line):
                found.append((shown, number, rule, text))
        if ID_TOKEN.search(line) and not docs_site:
            found.append((shown, number, "id-token: write outside docs-site.yml", text))
        if PAGES.search(line) and not docs_site:
            found.append((shown, number, "pages: write outside docs-site.yml", text))
        for uses in USES.finditer(line):
            action = uses.group("action").lower()
            name = action.split("@", 1)[0]
            if "login" in name:
                found.append((shown, number, "registry login (action)", text))
            if any(name.startswith(publish) for publish in PUBLISH_ACTIONS):
                found.append((shown, number, "package publish (action)", text))
            elif "release" in name:
                found.append((shown, number, "release asset: a third-party release action", text))
        for push in PUSH_KEY.finditer(line):
            value = (push.group("value") or "").strip().strip("'\"").lower()
            if value and value != "false" and not value.startswith(("{", "[")):
                found.append((shown, number, "push: other than false", text))
        if CACHE_TO_KEY.search(line):
            found.append((shown, number, "cache export (cache-to:)", text))
        for exporter in EXPORTER.finditer(line):
            if "cache-from" not in line[: exporter.start()].lower():
                found.append((shown, number, f"push (a registry exporter: {exporter.group('spec')})", text))
        for expression in EXPRESSION.finditer(line):
            body = expression.group("body")
            for context in SECRETS_CONTEXT.finditer(body):
                rest = body[context.end() :]
                if ONLY_THE_TOKEN.match(rest):
                    continue
                named = SECRET_NAME.match(rest)
                what = (named.group("dot") or named.group("bracket")) if named else None
                rule = f"secret: secrets.{what}" if what else "secret: the secrets context, not by a name"
                found.append((shown, number, rule, text))
        if SECRETS_INHERIT.search(line):
            found.append((shown, number, "secret: secrets: inherit", text))

    for words in logical_lines(lines):
        found.extend(check_release_create(words, shown))
        plain = [word for word, _ in words]
        for command in commands(plain):
            line_of = _line_of(words, command)
            for index, word in enumerate(command):
                if word == "--push" or word.startswith("--push="):
                    found.append((shown, line_of(index), "push (--push)", word))
                if word == "--cache-to" or word.startswith("--cache-to="):
                    found.append((shown, line_of(index), "cache export (--cache-to)", word))
                name = word.rsplit("/", 1)[-1]
                following = verbs(command, index)
                if name in PUSHERS and "push" in following:
                    found.append((shown, line_of(index), f"push by {name}", " ".join(command[index:index + 4])))
                if name in ENGINES and "login" in following:
                    found.append((shown, line_of(index), f"registry login by {name}", " ".join(command[index:index + 4])))
                writes = REGISTRY_WRITES.get(name, frozenset()).intersection(following)
                if writes:
                    found.append((shown, line_of(index), f"registry write by {name} ({', '.join(sorted(writes))})", " ".join(command[index:index + 4])))
                if word == "imagetools" and "create" in following:
                    found.append((shown, line_of(index), "push (buildx imagetools create)", " ".join(command[index:index + 2])))
                if name == "twine" and "upload" in following:
                    found.append((shown, line_of(index), "package publish (twine upload)", "twine upload"))
                if name in PUBLISHERS and "publish" in following:
                    found.append((shown, line_of(index), f"package publish ({name} publish)", " ".join(command[index:index + 3])))
                if name == "gh" and command[index + 1 : index + 3] == ["release", "upload"]:
                    found.append((shown, line_of(index), "release asset (gh release upload)", "gh release upload"))
    return found


def _line_of(words: list[tuple[str, int]], command: list[str]):
    """Map an index in ``command`` back to its line: the command is a run
    of ``words`` in order, so find where it starts."""
    plain = [word for word, _ in words]
    for start in range(len(plain) - len(command) + 1):
        if plain[start : start + len(command)] == command:
            return lambda index, start=start: words[start + index][1]
    return lambda index: words[0][1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", default=".", help="the checkout to read (default: here)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    workflows = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    actions = sorted((root / ".github" / "actions").glob("**/action.y*ml"))
    if not workflows:
        print(
            f"::error::no-publication read no workflow under {root}/.github/workflows. "
            "A guard that read nothing has proved nothing."
        )
        return 1
    findings = []
    for path in workflows:
        findings.extend(check_file(path, root, workflow=True))
    for path in actions:
        findings.extend(check_file(path, root, workflow=False))
    if findings:
        for shown, number, rule, text in sorted(set(findings), key=lambda f: (f[0], f[1], f[2])):
            print(f"{shown}:{number}: {rule}" + (f" — {text}" if text else ""))
        print(
            f"::error::no-publication: {len(set(findings))} finding(s) above. A LibreRun release "
            "publishes source and nothing else (L37): no workflow may hold the permission, the "
            "login, the push, the publish, the asset or the secret that would change that."
        )
        return 1
    for path in workflows + actions:
        print(f"read {path.relative_to(root)}")
    print(
        f"clean: {len(workflows)} workflow(s) and {len(actions)} action(s) can publish nothing "
        "(no publishing permission, no login, push, publish, asset or secret)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
