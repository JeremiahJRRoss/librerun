"""One VERSION, spelled canonically for pip (`scripts/check_single_version.py`, D38).

The converter A2 left dropped the hyphen and nothing else: `1.1.0-beta.1`
came out `1.1.0beta.1` — valid PEP 440, which `pip show` prints as
`1.1.0b1` — and `1.1.0-preview.1` passed through untouched. D38 replaced
it with a table, and these cases hold it: each label, dotted and not, and
a plain release, against what `packaging`, pip's own parser, prints; and
a pre-release outside the table stops the check, naming what it met.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_single_version.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_single_version", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("semver", "pep440"),
    [
        ("1.0.0", "1.0.0"),
        ("1.1.0-alpha.1", "1.1.0a1"),
        ("1.1.0-alpha1", "1.1.0a1"),
        ("1.1.0-a.2", "1.1.0a2"),
        ("1.1.0-a2", "1.1.0a2"),
        ("1.1.0-beta.1", "1.1.0b1"),
        ("1.1.0-beta1", "1.1.0b1"),
        ("1.1.0-b.3", "1.1.0b3"),
        ("1.1.0-b3", "1.1.0b3"),
        ("1.0.0-rc.1", "1.0.0rc1"),
        ("1.0.0-rc1", "1.0.0rc1"),
        ("2.0.0-beta.10", "2.0.0b10"),
    ],
)
def test_pep440_of_writes_the_canonical_form(semver, pep440):
    written = _load().pep440_of(semver)
    assert written == pep440
    # Canonical is what pip prints: its parser writes the spelling back
    # unchanged, and reads the semver spelling as the same version.
    assert str(Version(written)) == written
    assert Version(semver) == Version(written)


@pytest.mark.parametrize(
    ("semver", "named"),
    [
        ("1.1.0-preview.1", "'preview'"),
        ("1.1.0-pre.1", "'pre'"),
        ("1.1.0-c.1", "'c'"),
        ("1.1.0-dev.1", "'dev'"),
        ("1.1.0-BETA.1", "'BETA'"),
        ("1.1.0-beta", "'beta'"),
        ("1.1.0-beta.1.2", "'beta.1.2'"),
        ("1.1.0-beta.01", "'beta.01'"),
    ],
)
def test_a_label_outside_the_scheme_is_refused(semver, named, tmp_path, monkeypatch, capsys):
    """Why this goes red rather than writing something: `1.1.0beta.1` and
    `1.1.0-preview.1` are valid PEP 440, and pip re-normalises both, to
    `1.1.0b1` and `1.1.0rc1`, so a distribution carrying either says one
    version and `pip show` another. The converter A2 left wrote the first
    for `1.1.0-beta.1` and let the second through. The check demands the
    canonical spelling rather than a spelling pip rewrites, so every
    pre-release outside the table — each case here is either not PEP 440
    or one pip respells — stops it, naming what it met, before it reads a
    single carrier.
    """
    try:
        respelled = str(Version(semver))
    except InvalidVersion:
        pass
    else:
        assert respelled != semver, "a case pip would keep proves nothing here"

    module = _load()
    with pytest.raises(module.OutsideTheScheme, match=re.escape(named)):
        module.pep440_of(semver)

    # The check stops on VERSION alone: this root holds nothing else.
    (tmp_path / "VERSION").write_text(f"{semver}\n")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["check_single_version.py"])
    assert module.main() == 1
    out = capsys.readouterr().out
    assert f"::error::VERSION is {semver!r}: " in out
    assert named in out
