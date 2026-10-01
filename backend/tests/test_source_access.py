"""K blueprint A1 (gate R17): a running LibreRun says which licence it is
under and where the source of the version it runs is.

AGPL-3.0 section 13 asks whoever runs a modified LibreRun for people who
use it over a network to offer them the Corresponding Source of that
version. ``GET /api/v1/meta`` carries the two facts the offer rests on,
``license`` and ``source_url``, and the login page and the navigation bar
link to the second (``frontend/src/lib/__tests__/sourceLink.test.ts``).
This module holds the backend half:

* the default ``source_url`` is the public repository at the running
  version's tag, never ``main``, which moves on without the deployment;
* an operator's ``LIBRERUN_SOURCE_URL`` wins, and a blank one, which is
  what compose's ``${LIBRERUN_SOURCE_URL:-}`` delivers when ``.env`` is
  silent, means the default;
* the body carries exactly its keys, and no other setting's value;
* ``__license__`` is ``REUSE.toml``'s default, read from the map itself,
  so the answer and the licence scope map cannot drift apart.

Two files outside ``backend/`` are read here, ``README.md`` and
``REUSE.toml``, so both are in ``unit-suites.yml``'s ``paths:`` filters:
no guard derives those from what a test opens.
"""
from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from pydantic import SecretStr

import app.config
import app.main as main
import app.redis
from app.config import Settings
from app.version import __license__, __version__
from tests.test_demo_mode import _meta_client

ROOT = Path(__file__).resolve().parents[2]

# Every key the body carries. The endpoint is public — anyone can read it
# before signing in — so a new key is a new public fact about every
# deployment, decided here in review and never by accident.
META_KEYS = {
    "name",
    "version",
    "license",
    "source_url",
    "demo",
    "stub_llm",
    "gateway",
    "default_secret",
    "trace_viewer_configured",
    "trace_viewer",
    "trace_viewer_source",
    "agents",
}

# The two settings whose values the body carries by design: the source
# URL the operator set, and the trace viewer's preset name. Every other
# string setting is the deployment's own business.
NAMED_BY_THE_BODY = {"LIBRERUN_SOURCE_URL", "TRACE_VIEWER"}


@pytest.fixture
def meta(monkeypatch):
    """``GET /api/v1/meta`` through the real route, as a callable.

    The gateway's ``/healthz`` is stubbed, which is how keyless mode
    reaches this process in production anyway; the health cache and the
    Redis client are module globals the handler may fill, so both are
    restored when the test ends rather than left for the next one.
    """
    from app.services import gateway_client

    async def _health(timeout: float = 2.0):
        return {"status": "ok", "stub": False}

    monkeypatch.setattr(gateway_client, "health", _health)
    monkeypatch.setattr(main, "_gateway_health_cache", None)
    monkeypatch.setattr(app.redis, "redis_client", app.redis.redis_client)

    def get() -> dict:
        response = _meta_client(monkeypatch).get("/api/v1/meta")
        assert response.status_code == 200, response.text
        return response.json()

    return get


def _settings_from_the_environment(monkeypatch, value: str) -> None:
    """``LIBRERUN_SOURCE_URL`` as a container receives it, read by the real
    settings model: set in the process environment, which is where
    compose's interpolation puts it."""
    monkeypatch.setenv("LIBRERUN_SOURCE_URL", value)
    monkeypatch.setattr(app.config, "settings", Settings())


def _public_repository() -> str:
    """README.md's clone URL, less ``.git``: the public repository the
    default names, its owner segment the placeholder until it is
    resolved — in both places at once, or this module says so."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    found = set(re.findall(r"git clone (https://\S+?)\.git\b", text))
    assert len(found) == 1, f"README.md should clone from one URL; it shows {sorted(found)}"
    return found.pop()


def test_source_url_defaults_to_the_running_versions_tag(monkeypatch, meta):
    _settings_from_the_environment(monkeypatch, "")

    url = meta()["source_url"]

    assert url == f"{_public_repository()}/tree/v{__version__}"
    # The rule, stated apart from the spelling, so a failure reads as what
    # it is: the source of what is running is its tag, and `main` has
    # moved on without it.
    assert url.endswith(f"/tree/v{__version__}"), (
        f"the default source_url is {url!r}; it must name the running version's "
        f"tag, v{__version__}, and never a branch"
    )


def test_source_url_takes_the_operators_override(monkeypatch, meta):
    """An operator who runs modified source names where THEIR source is,
    and the answer is theirs exactly — no tag appended, nothing derived."""
    theirs = "https://git.example.com/your-org/librerun/src/tag/v9.9.9-ours"
    _settings_from_the_environment(monkeypatch, theirs)

    assert meta()["source_url"] == theirs


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_setting_means_the_default(monkeypatch, meta, blank):
    """compose's ``${LIBRERUN_SOURCE_URL:-}`` hands the backend an EMPTY
    variable, not an absent one, whenever ``.env`` is silent or sets the
    line blank. Blank has to mean the default, or every compose
    deployment would advertise no source at all."""
    _settings_from_the_environment(monkeypatch, blank)

    assert meta()["source_url"] == f"{_public_repository()}/tree/v{__version__}"


def test_meta_carries_exactly_its_keys(meta):
    body = meta()

    assert set(body) == META_KEYS, (
        "/api/v1/meta is public, so every key it carries is decided here. "
        f"Not decided: {sorted(set(body) - META_KEYS)}; "
        f"missing: {sorted(META_KEYS - set(body))}"
    )
    assert body["license"] == __license__


def test_meta_repeats_no_other_setting(monkeypatch, meta, tmp_path):
    """A canary in every string setting the body does not name — the
    secrets among them — and not one may appear anywhere in the body.

    A ``_FILE`` companion names a path, so it gets a canary path whose
    file holds the same canary as the variable beside it: both set and
    equal is the one pairing the ``_FILE`` rule accepts.
    """
    fields = Settings.model_fields
    strings = [
        name
        for name, field in fields.items()
        if field.annotation in (str, SecretStr) and name not in NAMED_BY_THE_BODY
    ]
    plain = [name for name in strings if not (name.endswith("_FILE") and name[:-5] in fields)]
    files = [name for name in strings if name not in plain]

    canaries: dict[str, str] = {}
    for name in plain:
        canaries[name] = f"canary{name.lower().replace('_', '')}q7"
        monkeypatch.setattr(app.config.settings, name, canaries[name])
    for name in files:
        path = tmp_path / f"canary{name.lower().replace('_', '')}q7"
        path.write_text(canaries[name[:-5]], encoding="utf-8")
        canaries[name] = str(path)
        monkeypatch.setattr(app.config.settings, name, str(path))

    # The census has to be the model, not a remembered list: a reader that
    # found nothing would pass by not looking.
    assert "APP_SECRET_KEY" in canaries and "DATABASE_URL_FILE" in canaries
    assert len(canaries) >= 40, sorted(canaries)
    assert app.config.settings.APP_SECRET_KEY.get_secret_value() == canaries["APP_SECRET_KEY"]

    text = json.dumps(meta())
    leaked = sorted(name for name, canary in canaries.items() if canary in text)

    assert leaked == [], (
        f"/api/v1/meta repeats the value of {leaked}; it is public, and carries "
        f"no setting but the ones its docstring names"
    )


def _glob(pattern: str) -> re.Pattern[str]:
    """A REUSE.toml path pattern: ``**`` crosses directories, ``*`` does not."""
    parts = re.split(r"(\*\*|\*)", pattern)
    return re.compile("".join({"**": ".*", "*": "[^/]*"}.get(p, re.escape(p)) for p in parts) + r"\Z")


def test_meta_license_is_the_maps_default(meta):
    """``__license__`` is a constant, not read from the map at run time —
    the image carries no root file — so this is what keeps the two equal.
    The default is what a first-party file nothing else claims takes: a
    new file at the root, and the module that declares the constant."""
    annotations = tomllib.loads((ROOT / "REUSE.toml").read_text(encoding="utf-8"))["annotations"]

    def licence_for(path: str) -> str | None:
        found = None
        for annotation in annotations:  # REUSE: the last match wins
            paths = annotation["path"]
            for pattern in [paths] if isinstance(paths, str) else paths:
                if _glob(pattern).match(path):
                    found = annotation["SPDX-License-Identifier"]
        return found

    default = licence_for("a-new-file-at-the-root.md")

    assert default is not None, "REUSE.toml no longer covers a new root file"
    assert licence_for("backend/app/version.py") == default
    assert __license__ == default, (
        f"app/version.py says {__license__!r}; REUSE.toml's default is {default!r}"
    )
    assert meta()["license"] == default
