#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Refresh the image digests R14 pins: the update procedure (D25).

For every image reference ``check_dependency_identity.py`` reads, this
resolves the digest of the multi-arch index the reference's tag points to
now, from two independent sources, and writes it only where they agree:

1. the image's registry, through its v2 API: a manifest ``HEAD`` with an
   anonymous pull token, asking for the OCI image index and the Docker
   manifest list, whose ``Docker-Content-Digest`` is the index's digest.
   A tag that serves one platform's manifest instead is not verified: its
   digest would pin that platform on every machine that pulls it;
2. for a Docker Hub image, Docker Hub's tags API, and ``mirror.gcr.io``'s
   copy of the same tag. Neither counts against Docker Hub's anonymous
   pull limit, one per-address bucket that a CI runner or a session's
   egress shares; a ``HEAD`` does not count either.

``--buildx`` adds ``docker buildx imagetools inspect`` as a third source
where an engine is at hand. A source that answers must agree with the
registry; one that cannot be reached is named and skipped; an image with
fewer than two agreeing sources is ``not verified``, and then nothing is
written at all: a digest is never guessed and never taken from a single
source.

    python3 scripts/refresh_image_digests.py           # dry run: what would move
    python3 scripts/refresh_image_digests.py --write   # rewrite the references

``--write`` rewrites each reference's digest and the ``Digest`` cell of its
``THIRD_PARTY.md`` row, with the row's note naming the date and the
sources, and never a tag: a tag is the line a refresh follows, moved only
by hand in a reviewed PR. A ``429`` is retried after the window the
registry names, never worked around by a login (R11). A refresh is a
reviewed pull request; record in it which sources answered.

Standard library only.
"""
from __future__ import annotations

import argparse
import datetime
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_dependency_identity as guard  # noqa: E402

ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
RESOLVED = re.compile(r"\s*Resolved on \d{4}-\d{2}-\d{2} from .+?, which agreed\.")
USER_AGENT = "librerun-refresh-image-digests"
TIMEOUT = 30
RETRIES = 3


class Unreachable(Exception):
    """A source that did not answer: named in the report, never a guess."""


def request(url: str, headers: dict[str, str] | None = None, method: str = "GET"):
    """An HTTP answer, a 429 retried after the window it names."""
    for attempt in range(RETRIES + 1):
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **(headers or {})}, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as answer:
                return answer.status, answer.headers, answer.read()
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < RETRIES:
                wait = int(err.headers.get("Retry-After") or 60)
                print(f"  429 from {url.split('/')[2]}: waiting {wait} s, as it asks")
                time.sleep(min(wait, 3600))
                continue
            return err.code, err.headers, b""
        except (urllib.error.URLError, TimeoutError, OSError) as err:
            raise Unreachable(f"{url.split('/')[2]}: {err}") from err
    raise Unreachable(f"{url.split('/')[2]}: still 429 after {RETRIES} waits")


def registry_of(name: str) -> tuple[str, str]:
    """The v2 API's host and the repository path for ``name``."""
    host, _, repo = name.partition("/")
    if host == "docker.io":
        host = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    return host, repo


def v2_digest(host: str, repo: str, tag: str) -> tuple[str, str]:
    """The digest and media type a v2 registry serves for ``repo:tag``,
    through the anonymous token its challenge names."""
    url = f"https://{host}/v2/{repo}/manifests/{tag}"
    status, headers, _ = request(url, {"Accept": ACCEPT}, "HEAD")
    if status == 401:
        challenge = headers.get("WWW-Authenticate") or ""
        realm = re.search(r'realm="([^"]+)"', challenge)
        if not realm:
            raise Unreachable(f"{host}: a 401 without a bearer challenge")
        service = re.search(r'service="([^"]+)"', challenge)
        query = f"scope=repository:{repo}:pull" + (f"&service={service.group(1)}" if service else "")
        status, _, body = request(f"{realm.group(1)}?{query}")
        if status != 200:
            raise Unreachable(f"{host}: the anonymous token answered {status}")
        token = json.loads(body).get("token") or json.loads(body).get("access_token")
        status, headers, _ = request(
            url, {"Accept": ACCEPT, "Authorization": f"Bearer {token}"}, "HEAD"
        )
    if status != 200:
        raise Unreachable(f"{host}: the manifest answered {status}")
    digest = headers.get("Docker-Content-Digest") or ""
    if not guard.DIGEST.fullmatch(digest):
        raise Unreachable(f"{host}: no sha256 Docker-Content-Digest")
    return digest, (headers.get("Content-Type") or "").split(";")[0]


def hub_digest(repo: str, tag: str) -> str:
    status, _, body = request(f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}")
    if status != 200:
        raise Unreachable(f"hub.docker.com: the tag answered {status}")
    digest = json.loads(body).get("digest") or ""
    if not guard.DIGEST.fullmatch(digest):
        raise Unreachable("hub.docker.com: no sha256 digest for the tag")
    return digest


def buildx_digest(ref: str) -> str:
    try:
        out = subprocess.run(
            ["docker", "buildx", "imagetools", "inspect", ref, "--format", "{{json .Manifest}}"],
            capture_output=True, text=True, timeout=120, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as err:
        raise Unreachable(f"buildx: {err}") from err
    digest = json.loads(out).get("digest") or ""
    if not guard.DIGEST.fullmatch(digest):
        raise Unreachable("buildx: no sha256 digest")
    return digest


def resolve(name: str, tag: str, buildx: bool) -> tuple[str | None, list[str], list[str], str]:
    """``(digest or None, sources that agreed, notes, media type)``."""
    host, repo = registry_of(name)
    notes, agreed = [], []
    try:
        digest, media = v2_digest(host, repo, tag)
    except Unreachable as err:
        return None, [], [f"the registry did not answer: {err}"], ""
    if media not in INDEX_TYPES:
        # One platform's manifest would pin that platform for every machine
        # that pulls or builds from the reference: R14 pins the index.
        return None, [], [f"the registry serves {media or 'no media type'} for the tag, not a multi-arch index"], media
    agreed.append("the registry")
    others = []
    if host == "registry-1.docker.io":
        others += [("Docker Hub's tags API", lambda: hub_digest(repo, tag)),
                   ("mirror.gcr.io", lambda: v2_digest("mirror.gcr.io", repo, tag)[0])]
    if buildx:
        others.append(("buildx imagetools", lambda: buildx_digest(f"{name}:{tag}")))
    for label, source in others:
        try:
            other = source()
        except Unreachable as err:
            notes.append(f"{label} did not answer: {err}")
            continue
        if other != digest:
            return None, agreed, notes + [f"{label} says {other}, the registry {digest}"], media
        agreed.append(label)
    if len(agreed) < 2:
        return None, agreed, notes + ["no second source agreed"], media
    return digest, agreed, notes, media


def references(root: Path) -> list[guard.Reference]:
    """The references the guard reads, found as the guard finds them."""
    files = guard.tracked(root)
    builds, composes = set(guard.build_files(files)), set(guard.compose_files(files))
    refs = []
    for f in files:
        text_of = lambda: (root / f).read_text(encoding="utf-8")  # noqa: E731
        if f in builds:
            refs += guard.dockerfile_references(f, text_of())[0]
        elif f in composes:
            refs += guard.compose_references(f, text_of())
        elif re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", f):
            refs += guard.workflow_references(f, text_of())[0]
    return [r for r in refs if r.text and "$" not in r.text]


def rewrite_line(path: Path, line: int, old: str, new: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if lines[line - 1].count(old) != 1:
        raise SystemExit(f"{path}:{line}: expected {old} once on the line; nothing more written")
    lines[line - 1] = lines[line - 1].replace(old, new)
    path.write_text("".join(lines), encoding="utf-8")


def rewrite_third_party(root: Path, name: str, tag: str, digest: str, sources: list[str]) -> bool:
    """Set the Digest cell of ``name``'s row at ``tag``, and its note's
    resolution sentence. False when THIRD_PARTY.md has no such row."""
    path = root / "THIRD_PARTY.md"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    header, hit, changed = None, False, False
    today = datetime.date.today().isoformat()
    said = ", ".join(sources[:-1]) + f" and {sources[-1]}" if len(sources) > 1 else sources[0]
    sentence = f"Resolved on {today} from {said}, which agreed."
    for i, raw in enumerate(lines):
        if not raw.startswith("|"):
            header = None
            continue
        cells = [c.strip() for c in raw.strip().strip("|").split("|")]
        if header is None:
            header = cells
            continue
        if set(raw.strip()) <= set("|-: ") or header[:3] != ["Image", "Tag", "Digest"]:
            continue
        row = dict(zip(header, cells))
        if row["Image"] != name or row["Tag"] != tag:
            continue
        hit = True
        if row["Digest"] == digest:
            continue
        row["Digest"] = digest
        # The last refresh's sentence goes, whatever its sources' names
        # hold (mirror.gcr.io has dots), and the note before it stays a
        # sentence of its own.
        note = RESOLVED.sub("", row.get("Notes", "")).strip()
        if note and not note.endswith((".", "!", "?")):
            note += "."
        row["Notes"] = f"{note} {sentence}" if note else sentence
        lines[i] = "| " + " | ".join(row[h] for h in header) + " |\n"
        changed = True
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return hit


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="rewrite the references (default: dry run)")
    parser.add_argument("--buildx", action="store_true", help="also ask docker buildx imagetools")
    parser.add_argument("--root", default=".", help="the checkout to read (default: here)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    refs = references(root)
    if not refs:
        print("::error::refresh-image-digests found no image reference: nothing was read.")
        return 1
    wanted = sorted({guard.split_reference(r.text)[:2] for r in refs})
    resolved, failed = {}, []
    for name, tag in wanted:
        if not tag or tag == "latest" or not guard.qualified(name):
            failed.append(f"{name}:{tag}: no tag a refresh can follow, or not fully qualified")
            continue
        digest, sources, notes, media = resolve(name, tag, args.buildx)
        for note in notes:
            print(f"  {name}:{tag}: {note}")
        if not digest:
            failed.append(f"{name}:{tag}: not verified")
            continue
        resolved[(name, tag)] = (digest, sources)
        print(f"{name}:{tag} -> {digest} ({', '.join(sources)}; {media.rsplit('.', 2)[-2] if media else '?'})")
    if failed:
        for line in failed:
            print(f"::error::{line}")
        print("::error::nothing was written: every digest comes from two agreeing sources, or none does")
        return 1
    moves = 0
    for r in refs:
        name, tag, current = guard.split_reference(r.text)
        digest, sources = resolved[(name, tag)]
        if current == digest:
            continue
        moves += 1
        print(f"{r.path}:{r.line}: {r.text} -> {name}:{tag}@{digest}")
        if args.write:
            rewrite_line(root / r.path, r.line, r.text, f"{name}:{tag}@{digest}")
    if args.write:
        for (name, tag), (digest, sources) in resolved.items():
            rewrite_third_party(root, name, tag, digest, sources)
    verb = "rewrote" if args.write else "would rewrite"
    print(f"{verb} {moves} reference(s); {len(resolved)} image(s) resolved, each from at least two agreeing sources")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
