"""``docs/authoring/Manifest.md`` is generated from the manifest models
and CI fails when the two differ (blueprint S6).

The page is a rendering of ``backend/app/agents/manifest.py`` by
``scripts/export_manifest_reference.py``: every model, every field with
its type, default and constraints, and the prose the module carries.
A reference typed by hand goes stale exactly when the thing it
describes changes; a generated one cannot, provided something checks
that the committed page IS the generator's output. This is that check,
and beside it the negative probe every guard needs: a change to a model
turns the check red, so a checker that stopped comparing would be
caught here rather than by the next reader of a stale page.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
SCRIPT = REPO / "scripts" / "export_manifest_reference.py"
PAGE = REPO / "docs" / "authoring" / "Manifest.md"


def _generator():
    spec = importlib.util.spec_from_file_location("export_manifest_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_page_is_the_generators_output():
    """`--check` from backend/, exactly as the docstring tells an author
    to run it — the subprocess is the CI command, not a re-implementation."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        cwd=str(BACKEND), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"docs/authoring/Manifest.md is out of date with the manifest models:\n"
        f"{result.stderr}\nRegenerate: cd backend && python ../scripts/export_manifest_reference.py"
    )
    assert PAGE.read_text(encoding="utf-8") == _generator().render()


def test_every_model_and_every_field_is_on_the_page():
    """The page has to say everything the models say: a model or a
    field the generator skipped would be a hole nobody notices."""
    from app.agents import manifest as m

    page = PAGE.read_text(encoding="utf-8")
    generator = _generator()
    models = generator._ordered_models()
    assert m.AgentManifest in models and m.LlmStepSpec in models and m.PhaseSpec in models
    for model in models:
        assert f"## `{model.__name__}`" in page, model.__name__
        for name in model.model_fields:
            assert f"| `{name}` |" in page, (model.__name__, name)
    for reserved in m.RESERVED_STEP_IDS:
        assert f"`{reserved}`" in page


def test_a_changed_model_turns_the_check_red(monkeypatch):
    """The negative probe: the check must FAIL when a model changes and
    the page does not. A docstring is the cheapest thing to change and
    the page renders it, so it is the injection."""
    from app.agents import manifest as m

    generator = _generator()
    assert generator.render() == PAGE.read_text(encoding="utf-8")
    monkeypatch.setattr(m.PhaseSpec, "__doc__", (m.PhaseSpec.__doc__ or "") + "\n\nInjected drift.")
    changed = generator.render()
    assert changed != PAGE.read_text(encoding="utf-8")
    assert "Injected drift." in changed


def test_the_generator_reads_the_comment_above_a_field():
    """Field prose comes from the comment block written directly above
    the declaration — the one place the module already explains a field
    — so a new comment reaches the page without anyone retyping it."""
    from app.agents import manifest as m

    comments = _generator()._field_comments(m)
    assert ("AgentManifest", "scenarios") in comments
    assert "scenario JSON files" in comments[("AgentManifest", "scenarios")]
    # A field with no comment above it gets none — never a neighbour's.
    assert ("PhaseSpec", "approval") not in comments
