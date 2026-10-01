"""The GitHub Release body for a tag (`scripts/release_notes.py`, S9; S10).

Nothing tested the script before S10, which is how the v1.0.0 page came
within one tag of carrying a literal `ghcr.io/<owner>/…`: the footer
never named the registry the images were pushed to. Since A2 a release is source
only, and the notes name no registry at all. These cases drive the real
`main()` against the real `CHANGELOG.md`, the way `release.yml` runs it,
and against a small changelog where the shape of the answer has to be
exact.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_notes.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _notes(monkeypatch, capsys, tag, *, owner=None, changelog=None, root=None):
    module = _load()
    if root is not None:
        monkeypatch.setattr(module, "ROOT", root)
    if owner is None:
        monkeypatch.delenv("GITHUB_REPOSITORY_OWNER", raising=False)
    else:
        monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", owner)
    monkeypatch.setattr("sys.argv", ["release_notes.py", tag])
    assert module.main() == 0
    return capsys.readouterr().out


def test_the_footer_says_the_release_is_source_only(monkeypatch, capsys):
    # A2 (L37, D26): a release publishes source and nothing else, so the
    # footer says how to build it where it named images to pull.
    out = _notes(monkeypatch, capsys, "v1.0.0", owner="some-org")
    footer = out.split("\n---\n", 1)[1]
    paragraphs = [" ".join(p.split()) for p in footer.split("\n\n")]
    source = [p for p in paragraphs if p.startswith("Source only")]
    assert source == [
        "Source only: build with `./scripts/demo.sh`; the CLI and SDK install from the "
        "tree. No image, wheel or package is published: the tag's source archive is "
        "the release (`docs/platform/Releasing.md`)."
    ], paragraphs
    for gone in ("Container images", "ghcr.io", "--pull"):
        assert gone not in footer, gone


def test_the_footer_names_the_platform_licence(monkeypatch, capsys):
    # #147 rewrote the footer for L36 and L40, and nothing pinned it, so the
    # earlier wording could come back with every check green (K blueprint
    # A1). The licence paragraph, as one line: the platform licence first,
    # then the carve-outs, the third-party notices, the name, the DCO.
    out = _notes(monkeypatch, capsys, "v1.0.0", owner="some-org")
    footer = out.split("\n---\n", 1)[1]
    paragraphs = [" ".join(p.split()) for p in footer.split("\n\n")]
    licence = [p for p in paragraphs if p.startswith("Licensed under")]
    assert len(licence) == 1, paragraphs
    assert licence[0].startswith("Licensed under AGPL-3.0-only, "), licence[0]
    for fact in ("four Apache-2.0 directories", "`NOTICE`", "`TRADEMARKS.md`",
                 "DCO sign-off", "`CONTRIBUTING.md`"):
        assert fact in licence[0], (fact, licence[0])


@pytest.mark.parametrize("tag", ["v1.0.0", "v1.1.0"])
def test_the_notes_name_no_registry_whatever_the_owner(monkeypatch, capsys, tmp_path, tag):
    # The owner went with the images (A2): nothing reads it, so a canary
    # owner changes no byte of the notes. The frozen 1.0.0 section still
    # quotes `ghcr.io/<owner>` in its own text, so the registry is looked
    # for in what the script writes, around a changelog of one line.
    canary = "Canary-Owner-5e1f"
    out = _notes(monkeypatch, capsys, tag, owner=canary)
    assert out == _notes(monkeypatch, capsys, tag)
    assert canary.lower() not in out.lower()
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [{tag[1:]}]\n\n### Added\n- a thing\n"
    )
    written = _notes(monkeypatch, capsys, tag, owner=canary, root=tmp_path)
    for registry in ("ghcr.io", "docker.io", "quay.io", "<owner>", "registry"):
        assert registry not in written.lower(), registry


def test_v1_0_0_takes_the_frozen_section_and_leads_with_its_highlights(monkeypatch, capsys):
    # S10 froze `## [1.0.0]`, so the notes come from it rather than from
    # [Unreleased] — and because GitHub caps a release body, the section
    # opens with what a reader needs before the batch-by-batch detail.
    out = _notes(monkeypatch, capsys, "v1.0.0", owner="some-org")
    assert out.startswith("## LibreRun 1.0.0\n")
    assert "Pre-release" not in out
    assert "[Unreleased]" not in out
    head = out[:6000]
    assert "Highlights" in head
    assert "docs/release/v1.0.0.md" in head


def test_the_beta_takes_its_dated_section_and_leads_with_its_highlights(monkeypatch, capsys):
    # B1b dated [Unreleased] as the beta's own section, an empty
    # [Unreleased] above it (C15). Against the real CHANGELOG, the way
    # release.yml runs it: the notes for the beta's tag come from that
    # section, never from the empty one above or from 1.0.0 below; they
    # open with the beta's heading and say it is a pre-release; and they
    # lead with the Highlights that point at the announcement, because a
    # release body is capped — and carry no `#N` GitHub would link.
    out = _notes(monkeypatch, capsys, "v1.1.0-beta.1")
    assert out.startswith("## LibreRun 1.1.0-beta.1\n\n**Pre-release: a beta.** Not for production.")
    # Not the fallback, whose preamble names [Unreleased] (B1a's entry in the
    # section names it too, so the bare word proves nothing).
    assert "These notes are the **[Unreleased]** section" not in out
    module = _load()
    heading, body = module.section((ROOT / "CHANGELOG.md").read_text(), r"^## \[1\.1\.0-beta\.1\]")
    assert re.fullmatch(r"## \[1\.1\.0-beta\.1\] — \d{4}-\d{2}-\d{2}", heading), heading
    assert body.startswith("### Highlights\n"), body[:80]
    head = out[:6000]
    assert "### Highlights" in head and "docs/release/v1.1.0-beta.1.md" in head
    assert "### Changed — the licence reconciliation" in out
    assert "### Deprecated — B1b" in out
    assert "S1 — The chassis speaks `run`" not in out
    assert _linkable(out) == []


def test_a_candidate_is_marked_and_says_where_its_notes_came_from(monkeypatch, capsys, tmp_path):
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- a thing\n\n## [0.9.0]\n- old\n"
    )
    out = _notes(monkeypatch, capsys, "v1.1.0-rc1", owner="some-org", root=tmp_path)
    assert "**Pre-release: a release candidate.**" in out
    assert "[Unreleased]" in out and "- a thing" in out
    assert "- old" not in out


@pytest.mark.parametrize("size", [59_000, 61_000])
def test_the_body_is_capped_and_says_so(monkeypatch, capsys, tmp_path, size):
    line = "x" * 99 + "\n"
    body = line * (size // len(line))
    (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n## [2.0.0] — 2027-01-01\n\n{body}")
    out = _notes(monkeypatch, capsys, "v2.0.0", owner="some-org", root=tmp_path)
    truncated = "truncated here" in out
    assert truncated == (size > 60_000)
    assert len(out) < 62_000


def test_a_beta_tag_takes_its_dated_section(monkeypatch, capsys, tmp_path):
    # B1b dates [Unreleased] as the beta's own section and opens an empty
    # [Unreleased] above it (C15): the beta's notes come from its section,
    # date and all, never from the empty one above, and the header calls
    # it a beta rather than a candidate.
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n"
        "## [1.1.0-beta.1] — 2026-10-02\n\n### Added\n- the beta's thing\n\n"
        "## [1.0.0] — certified 2026-09-22, never published\n\n- the release's thing\n"
    )
    out = _notes(monkeypatch, capsys, "v1.1.0-beta.1", root=tmp_path)
    assert out.startswith("## LibreRun 1.1.0-beta.1\n\n**Pre-release: a beta.** Not for production.")
    assert "### Added\n- the beta's thing" in out
    assert "the release's thing" not in out
    assert "[Unreleased]" not in out
    assert "candidate" not in out


def test_the_fallback_promises_no_frozen_release(monkeypatch, capsys, tmp_path):
    # A2's fallback said the changelog "is not frozen until 1.0.0": a
    # promise about a release that was certified and never published. A
    # tag with no section of its own says where its notes came from and
    # when a section is dated, and promises nothing about a frozen one.
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n\n### Added\n- a thing\n\n"
        "## [1.0.0] — certified 2026-09-22, never published\n\n- old\n"
    )
    out = _notes(monkeypatch, capsys, "v1.1.0-beta.2", root=tmp_path)
    quoted = " ".join(line[2:] for line in out.splitlines() if line.startswith("> "))
    assert quoted.startswith(
        "These notes are the **[Unreleased]** section of `CHANGELOG.md` as it stood at "
        "`v1.1.0-beta.2`."
    ), quoted
    assert "a section is dated when its version is released" in quoted
    for promise in ("frozen", "1.0.0", "candidate"):
        assert promise not in quoted, promise
    assert "- a thing" in out and "- old" not in out


def _linkable(markdown: str) -> list[str]:
    """Each `#N` a GitHub Release body would link: in rendered text, not in
    code. CommonMark decides what is code — markdown-it-py, which the
    backend's lock carries, is its reference port — so this reads the notes
    the way the renderer does rather than the way the script does."""
    from markdown_it import MarkdownIt

    found: list[str] = []
    for block in MarkdownIt("commonmark").parse(markdown):
        for token in block.children or []:
            if token.type == "text":
                found += re.findall(r"(?<![\w&#/])#\d+\b", token.content)
    return found


def test_an_issue_number_does_not_link_in_the_public_repository(monkeypatch, capsys, tmp_path):
    # A GitHub Release body links `#N` to issue N of the repository it is
    # published in, the public one, while the changelog's numbers are the
    # development record's: the 1.0.0 section carries five. Each `#N`
    # outside a code span goes into one, whose backtick run pairs with
    # nothing it did not open; a span, a fence, a link's destination, an
    # entity and a number inside a word or a path stay as they are.
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [1.1.0-beta.1] — 2026-10-02\n\n"
        "### Fixed — the thing (PR #49)\n\n"
        "- a sink fixed (#81); issues #84 and\n"
        "  #85, and `#86` in a span, ``a `#87` in a double one``.\n"
        "- [the gates](docs/x.md#12-gates), &#35;, x#88 and a/#89.\n"
        "- an unclosed ` backtick, then #90\n\n"
        "```\n#91 in a fence\n```\n"
    )
    out = _notes(monkeypatch, capsys, "v1.1.0-beta.1", root=tmp_path)
    assert _linkable(out) == []
    for wrapped in ("(PR `#49`)", "(`#81`); issues `#84` and\n  `#85`,",
                    "an unclosed ` backtick, then ``#90``"):
        assert wrapped in out, wrapped
    for kept in ("and `#86` in a span, ``a `#87` in a double one``.",
                 "(docs/x.md#12-gates), &#35;, x#88 and a/#89.", "```\n#91 in a fence\n```"):
        assert kept in out, kept

    # And the record the rule is for: the five in the 1.0.0 section.
    module = _load()
    _, body = module.section((ROOT / "CHANGELOG.md").read_text(), r"^## \[1\.0\.0\]")
    assert len(_linkable(body)) == 5, _linkable(body)
    assert _linkable(module.unlinked(body)) == []
