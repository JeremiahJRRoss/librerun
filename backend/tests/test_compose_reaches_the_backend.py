"""A setting the backend REPORTS has to reach the backend (S4a).

`/admin/settings` shows an effective value for every row, and some of
those defaults are computed from this process's environment. S4a moved
`LIBRERUN_KB_EMBED_MODEL` to the gateway — correctly, since the gateway
is what enforces it — and passed it there and nowhere else. The backend
kept computing `kb.embed_model`'s default from its own environment, so
an operator who set the variable saw the built-in model on the settings
page while the gateway routed theirs (Codex P2).

The guard is the class, not the instance: every `env_settings.X` a
settings default reads must be in the backend service's environment.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]


def env_names_read_by_settings_defaults() -> set[str]:
    """Every ``env_settings.NAME`` a default_factory reads, found in the
    source rather than listed here — a list would be the thing that goes
    stale.

    Three spellings since K6, and the scan reads all of them: the module's
    import-time ``env_settings.NAME``; ``_config.settings.NAME``, which the
    sign-in settings' defaults use to read the current settings object;
    and a secret setting's ``env_var="NAME"``, the variable its value
    falls back to (L29). A default read any other way would be one this
    guard cannot see, which is the same as one it does not check."""
    source = (REPO / "backend/app/services/app_settings_service.py").read_text()
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "env_settings"
        ):
            names.add(node.attr)
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "settings"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "_config"
        ):
            names.add(node.attr)
        if (
            isinstance(node, ast.keyword)
            and node.arg == "env_var"
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            names.add(node.value.value)
    return names


def compose_environment(service: str) -> set[str]:
    document = yaml.safe_load((REPO / "compose.yaml").read_text())
    environment = (document["services"][service].get("environment") or {})
    if isinstance(environment, list):
        return {item.split("=", 1)[0] for item in environment}
    return set(environment)


def test_the_scan_finds_the_defaults_it_is_about():
    """The guard is worthless if its scan finds nothing."""
    names = env_names_read_by_settings_defaults()

    assert "LIBRERUN_KB_EMBED_MODEL" in names
    assert len(names) >= 4
    # K6's two spellings, each by a name only it reads.
    assert "GOOGLE_CLIENT_ID" in names, "the scan misses _config.settings.NAME"
    assert "AZURE_CLIENT_SECRET" in names, "the scan misses a secret's env_var"


def test_every_reported_default_reaches_the_backend():
    from app.config import Settings

    backend_env = compose_environment("backend")
    missing = [
        name
        for name in sorted(env_names_read_by_settings_defaults())
        # Only real settings fields: `cors_origins_list` and friends are
        # derived properties, and their source variable is what matters.
        if name in Settings.model_fields and name not in backend_env
    ]

    assert missing == [], (
        "the settings page computes these defaults from the backend's "
        "environment, but compose does not pass them to it: " + ", ".join(missing)
    )


def test_the_gateway_gets_it_too():
    """The gateway is what enforces the bound, so both need the value —
    this is not a case of moving it back."""
    assert "LIBRERUN_KB_EMBED_MODEL" in compose_environment("gateway")


def test_the_two_services_are_given_the_same_default():
    """One variable, one default expression. Two spellings would drift
    the moment somebody edited one of them."""
    text = (REPO / "compose.yaml").read_text()
    expressions = set(
        re.findall(r"LIBRERUN_KB_EMBED_MODEL:\s*(\$\{[^}]+\})", text)
    )

    assert len(expressions) == 1, expressions
