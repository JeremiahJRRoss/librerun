"""`scripts/refresh_image_digests.py` — the procedure R14's digests move by.

D25: a digest is never guessed and never taken from a single source, and
a refresh is a reviewed pull request whose `THIRD_PARTY.md` rows say when
and from where each digest was resolved. Every case here runs without
the network: the sources are stubbed, so each plants one answer and
requires the outcome.

The rewrite cases exist because A3's own refresh wrote four notes whose
resolution sentence ran on from the note before it ("… licences Resolved
on …"), and the pattern meant to replace an earlier sentence stopped at
the first dot of `mirror.gcr.io`, so the next refresh would have added a
second sentence instead of replacing the first.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "refresh_image_digests.py"

A = "sha256:" + "a" * 64
B = "sha256:" + "b" * 64
INDEX = "application/vnd.oci.image.index.v1+json"
THREE = ["the registry", "Docker Hub's tags API", "mirror.gcr.io"]


def _module():
    """The script as a module; `sys.path` as it was before, since the
    script puts its own directory first to import its guard."""
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location("refresh_image_digests", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


refresh = _module()


@pytest.fixture
def sources(monkeypatch):
    """Stub each source: a digest (a registry's may be `(digest, media
    type)`), or `None` for one that does not answer."""
    answers = {}

    def v2(host, repo, tag):
        answer = answers.get(host)
        if answer is None:
            raise refresh.Unreachable(f"{host}: stubbed silent")
        return answer if isinstance(answer, tuple) else (answer, INDEX)

    def hub(repo, tag):
        digest = answers.get("hub")
        if digest is None:
            raise refresh.Unreachable("hub.docker.com: stubbed silent")
        return digest

    monkeypatch.setattr(refresh, "v2_digest", v2)
    monkeypatch.setattr(refresh, "hub_digest", hub)
    return answers


def test_two_agreeing_sources_resolve_a_digest(sources):
    sources.update({"registry-1.docker.io": A, "hub": A})
    digest, agreed, notes, _ = refresh.resolve("docker.io/library/postgres", "16", False)
    assert digest == A
    assert agreed == ["the registry", "Docker Hub's tags API"]
    assert any(note.startswith("mirror.gcr.io did not answer") for note in notes)


def test_a_digest_is_never_taken_from_one_source(sources):
    sources.update({"registry-1.docker.io": A})
    digest, agreed, notes, _ = refresh.resolve("docker.io/library/postgres", "16", False)
    assert digest is None
    assert agreed == ["the registry"]
    assert notes[-1] == "no second source agreed"


@pytest.mark.parametrize(
    "answers, says",
    [
        ({"registry-1.docker.io": A, "hub": B, "mirror.gcr.io": A}, "Docker Hub's tags API says"),
        ({"registry-1.docker.io": A, "hub": A, "mirror.gcr.io": B}, "mirror.gcr.io says"),
    ],
)
def test_a_source_that_disagrees_stops_the_image(sources, answers, says):
    sources.update(answers)
    digest, _, notes, _ = refresh.resolve("docker.io/library/postgres", "16", False)
    assert digest is None
    assert notes[-1].startswith(says) and B in notes[-1]


@pytest.mark.parametrize("media", ["application/vnd.oci.image.manifest.v1+json", ""])
def test_a_tag_that_serves_one_platform_is_not_verified(sources, media):
    # Codex on #192: a single-platform digest, pinned by --write, would
    # fail every other architecture that builds or pulls the reference.
    sources.update({"registry-1.docker.io": (A, media), "hub": A, "mirror.gcr.io": A})
    digest, agreed, notes, _ = refresh.resolve("docker.io/library/postgres", "16", False)
    assert (digest, agreed) == (None, [])
    assert notes[-1].endswith("not a multi-arch index")


def test_an_image_whose_registry_does_not_answer_is_not_verified(sources):
    sources.update({"hub": A, "mirror.gcr.io": A})
    digest, agreed, notes, _ = refresh.resolve("docker.io/library/postgres", "16", False)
    assert (digest, agreed) == (None, [])
    assert notes[0].startswith("the registry did not answer")


TABLE = """\
| Image | Tag | Digest | Licence | Pulled by | Notes |
|---|---|---|---|---|---|
| docker.io/library/postgres | 16-alpine | {a} | PostgreSQL | compose | PostgreSQL 16 on Alpine; packages under their own licences |
| docker.io/library/python | 3.12-slim | {a} | PSF-2.0 | builds | CPython on Debian (copyright files in the image) |
| docker.io/library/caddy | 2.11.4 | {a} | Apache-2.0 | compose | Caddy. Resolved on 2026-09-28 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed. |
| docker.io/timberio/vector | 0.50.0-alpine | {a} | MPL-2.0 | compose |  |
"""


@pytest.fixture
def third_party(tmp_path, monkeypatch):
    (tmp_path / "THIRD_PARTY.md").write_text(TABLE.format(a=A), encoding="utf-8")
    today = types.SimpleNamespace(date=types.SimpleNamespace(today=lambda: datetime.date(2026, 9, 30)))
    monkeypatch.setattr(refresh, "datetime", today)
    return tmp_path


def _notes(root: Path) -> dict[str, str]:
    rows = {}
    for line in (root / "THIRD_PARTY.md").read_text(encoding="utf-8").splitlines()[2:]:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 6, line
        rows[cells[0]] = (cells[2], cells[5])
    return rows


def test_a_refresh_writes_one_resolution_sentence_after_the_note(third_party):
    for name, tag in [
        ("docker.io/library/postgres", "16-alpine"),
        ("docker.io/library/python", "3.12-slim"),
        ("docker.io/library/caddy", "2.11.4"),
        ("docker.io/timberio/vector", "0.50.0-alpine"),
    ]:
        assert refresh.rewrite_third_party(third_party, name, tag, B, THREE)
    said = "Resolved on 2026-09-30 from the registry, Docker Hub's tags API and mirror.gcr.io, which agreed."
    rows = _notes(third_party)
    assert rows["docker.io/library/postgres"] == (
        B, f"PostgreSQL 16 on Alpine; packages under their own licences. {said}")
    assert rows["docker.io/library/python"] == (
        B, f"CPython on Debian (copyright files in the image). {said}")
    assert rows["docker.io/library/caddy"] == (B, f"Caddy. {said}")
    assert rows["docker.io/timberio/vector"] == (B, said)


def test_the_next_refresh_replaces_the_sentence_rather_than_adding_one(third_party):
    name, tag = "docker.io/library/postgres", "16-alpine"
    refresh.rewrite_third_party(third_party, name, tag, B, THREE)
    refresh.rewrite_third_party(third_party, name, tag, A, ["the registry", "mirror.gcr.io"])
    digest, note = _notes(third_party)[name]
    assert digest == A
    assert note.count("Resolved on") == 1
    assert note.endswith(". Resolved on 2026-09-30 from the registry and mirror.gcr.io, which agreed.")


def test_an_unchanged_digest_leaves_the_file_as_it_was(third_party):
    before = (third_party / "THIRD_PARTY.md").read_bytes()
    assert refresh.rewrite_third_party(third_party, "docker.io/library/caddy", "2.11.4", A, THREE)
    assert (third_party / "THIRD_PARTY.md").read_bytes() == before


def test_an_image_with_no_row_is_reported(third_party):
    before = (third_party / "THIRD_PARTY.md").read_bytes()
    assert not refresh.rewrite_third_party(third_party, "docker.io/library/redis", "7-alpine", B, THREE)
    assert (third_party / "THIRD_PARTY.md").read_bytes() == before
