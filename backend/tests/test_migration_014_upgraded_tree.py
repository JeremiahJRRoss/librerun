"""The shape an UPGRADED installation actually has (Codex round 10, P2).

Migration 014 seeds a tenant override only where the persisted overlay
DIVERGES from the packaged defaults — decision 45's fix, so an upgrade
does not pin an agent's defaults for installations that never chose
them.

That fix compared the overlay against the packaged `config.json`'s
`pipeline` block. Then this same batch **moved that block into the
manifest**, so on the shipped tree `packaged` reads as `{}` — and on an
upgraded installation, whose persisted overlay still holds the OLD full
pipeline, every field differs from nothing and the pinning comes
straight back one release later.

The existing seeding tests never saw it because their synthetic trees
always write a `config.json` WITH a pipeline: they model the world
before the migration, not the package that ships with it. So this file
builds the post-migration shape on purpose — manifest steps, no packaged
pipeline, a pre-upgrade overlay — which is the only shape a real upgrade
ever encounters.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "014_agent_gateway_tables.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("_m014_upgraded", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m014 = _load_migration()

STEPS = {
    "think": {
        "provider": "openai",
        "model": "gpt-4o",
        "temperature": 0.0,
        "max_tokens": 1000,
        "timeout_seconds": 30,
    },
    "plan": {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "temperature": 0.5,
        "max_tokens": 2000,
        "timeout_seconds": 60,
    },
}


def _manifest_yaml(steps: dict) -> str:
    body = "manifest_version: 1\nid: probe-v1\nname: Probe\nruntime: python-package\n"
    body += "llm:\n  steps:\n"
    for step_id, values in steps.items():
        body += f"    - id: {step_id}\n"
        body += f"      provider: \"{values['provider']}\"\n"
        body += f"      model: \"{values['model']}\"\n"
        body += f"      temperature: {values['temperature']}\n"
        body += f"      max_tokens: {values['max_tokens']}\n"
        body += f"      timeout_seconds: {values['timeout_seconds']}\n"
    return body


@pytest.fixture
def upgraded(tmp_path, monkeypatch):
    """An installation as it looks the moment this batch is deployed:
    the package carries steps in the MANIFEST and no `pipeline` block,
    and the persisted state volume still carries the pre-upgrade
    overlay."""

    def _build(overlay_pipeline: dict | None):
        agents = tmp_path / "agents"
        state = tmp_path / "state"
        agent_dir = agents / "probe_v1"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "agent.yaml").write_text(_manifest_yaml(STEPS))
        # As shipped after this batch: everything BUT the pipeline.
        (agent_dir / "config.json").write_text(
            json.dumps({"search": {}, "feature_flags": {}})
        )
        if overlay_pipeline is not None:
            overlay = state / "agents" / "probe_v1"
            overlay.mkdir(parents=True, exist_ok=True)
            (overlay / "config.json").write_text(
                json.dumps({"pipeline": overlay_pipeline})
            )
        monkeypatch.setenv("LIBRERUN_AGENTS_PATH", str(agents))
        monkeypatch.setenv("LIBRERUN_STATE_DIR", str(state))
        return agent_dir

    return _build


def _would_seed(agent_dir) -> dict:
    """The override each step would be written with — the seeding's own
    arithmetic, so the guard cannot drift from it."""
    effective, packaged = m014._legacy_pipeline(agent_dir)
    out = {}
    for step_id, step in effective.items():
        default = packaged.get(step_id) or {}
        values = {
            field: (
                step.get(field)
                if step.get(field) is not None and step.get(field) != default.get(field)
                else None
            )
            for field in m014._STEP_FIELDS
        }
        seeded = {k: v for k, v in values.items() if v is not None}
        if seeded:
            out[step_id] = seeded
    return out


def test_the_comparison_base_survives_the_pipeline_moving_to_the_manifest(upgraded):
    """The base must be found where the defaults now LIVE, not where
    they used to."""
    agent_dir = upgraded(None)
    _effective, packaged = m014._legacy_pipeline(agent_dir)

    assert set(packaged) == set(STEPS), (
        "the packaged defaults were not found — every overlay value will "
        "look like a tenant override"
    )
    assert packaged["think"]["model"] == "gpt-4o"


def test_an_upgrade_that_changed_nothing_pins_nothing(upgraded):
    """The real regression: an admin with a persisted overlay holding a
    verbatim copy of the defaults (which is what an overlay contains
    after changing any unrelated setting) must inherit no overrides."""
    agent_dir = upgraded(dict(STEPS))

    assert _would_seed(agent_dir) == {}, (
        "an installation that changed nothing had its defaults pinned, so "
        "the agent author's next release can never reach it"
    )


def test_a_real_override_still_survives_the_upgrade(upgraded):
    """The other direction, and the reason this cannot simply seed
    nothing: an admin who DID choose a different model must keep it."""
    changed = {**STEPS, "think": {**STEPS["think"], "model": "gpt-4o-mini"}}
    agent_dir = upgraded(changed)

    assert _would_seed(agent_dir) == {"think": {"model": "gpt-4o-mini"}}


def test_an_agent_still_in_the_old_shape_is_unaffected(upgraded):
    """A third-party agent that has not migrated still carries its
    defaults in the packaged `config.json`; that file stays the base."""
    agent_dir = upgraded(dict(STEPS))
    (agent_dir / "config.json").write_text(json.dumps({"pipeline": dict(STEPS)}))

    _effective, packaged = m014._legacy_pipeline(agent_dir)
    assert packaged == STEPS
    assert _would_seed(agent_dir) == {}


def test_a_manifest_that_cannot_be_parsed_does_not_break_the_upgrade(upgraded):
    """A migration that raises on a malformed file leaves the database
    half-migrated, which is worse than seeding nothing for that agent."""
    agent_dir = upgraded(dict(STEPS))
    (agent_dir / "agent.yaml").write_text("id: probe-v1\nllm:\n  steps: [[[\n")

    effective, packaged = m014._legacy_pipeline(agent_dir)
    assert packaged == {}
    assert effective  # the overlay is still read
