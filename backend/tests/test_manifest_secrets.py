"""The manifest's ``secrets[]`` (K8a; D20, D35, L13, L23).

An agent names the tool secrets it may ask for, and only those: each is a
settings key, declared once, and never a name the platform reserves —
judged upper-cased, as the in-process fallback reads the environment. No
grant is needed to read one, so ``secrets`` is refused as a grant: it would
read as one that did.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agents.manifest import AgentManifest, load_manifest
from app.services.tool_secrets_service import reserved_reason

REPO = Path(__file__).resolve().parents[2]

BASE = {
    "id": "probe",
    "name": "Probe",
    "runtime": "python-package",
    "phases": [{"name": "work"}],
    "output": {"mode": "structured"},
}


def _manifest(**fields) -> AgentManifest:
    return AgentManifest.model_validate({**BASE, **fields})


def _refusal(**fields) -> str:
    with pytest.raises(ValidationError) as raised:
        _manifest(**fields)
    return str(raised.value)


def test_secrets_are_optional_and_kept_in_order():
    assert _manifest().secrets == []
    assert _manifest(secrets=["search_key", "vector_key"]).secrets == [
        "search_key",
        "vector_key",
    ]


@pytest.mark.parametrize(
    "name, why",
    [
        ("openai_api_key", "a model provider's key"),
        ("anthropic_api_key", "a model provider's key"),
        ("google_ai_api_key", "a model provider's key"),
        ("app_secret_key", "a setting of the backend's own"),
        ("database_url", "a setting of the backend's own"),
        ("pinecone_api_key", "a setting of the backend's own"),
        ("app_secret_key_file", "a setting of the backend's own"),
        ("platform_tenant_slug_file", "_FILE spelling"),
        ("librerun_anything", "LIBRERUN_ namespace"),
        ("otel_exporter_token", "OTEL_ namespace"),
    ],
)
def test_a_reserved_name_is_refused(name, why):
    """D35's denylist, and L23: the fallback must never hand an agent the
    platform's own value, and a provider key is the gateway's alone."""
    refusal = _refusal(secrets=[name])
    assert "reserved" in refusal and why in refusal, refusal
    assert reserved_reason(name) is not None


@pytest.mark.parametrize(
    "names, why",
    [
        (["Search_Key"], "not a lowercase key"),
        (["9lives"], "not a lowercase key"),
        (["search-key"], "not a lowercase key"),
        (["s" * 65], "longer than 64 characters"),
        (["search_key", "search_key"], "duplicate secrets"),
    ],
)
def test_the_names_are_settings_keys_declared_once(names, why):
    assert why in _refusal(secrets=names)


def test_secrets_is_not_a_grant():
    """Reading a tool secret needs no grant, so a grant named for them
    would read as one that did."""
    refusal = _refusal(capabilities=["secrets"])
    assert "'secrets' is not a capability" in refusal


def test_the_demo_agent_declares_its_search_key():
    """L13: the demo agent's key is its own declared tool secret, which a
    settings field of the backend's would reserve (test_secret_files.py
    holds that the field is gone and that putting it back fails this)."""
    manifest = load_manifest(REPO / "backend" / "agents" / "vita_v1")
    assert manifest.secrets == ["tavily_api_key"]
    assert reserved_reason("tavily_api_key") is None


def test_every_shipped_manifest_still_loads():
    """Every agent the tree ships validates with the new field in the model."""
    root = REPO / "backend" / "agents"
    paths = sorted([*root.glob("*/agent.yaml"), *root.glob("_examples/*/agent.yaml")])
    assert paths
    for path in paths:
        load_manifest(path.parent)
