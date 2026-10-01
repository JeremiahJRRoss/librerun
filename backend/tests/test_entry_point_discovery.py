"""Blueprint B12: agents install two ways — agents directory and pip
entry points (``librerun.agents``) — under one manifest contract.

Covers the new discovery surface:

* an installed entry point registers exactly like a directory agent
  (manifest required, assets resolved from the package directory);
* every entry-point failure mode (unloadable, not-a-module, manifest
  missing) is a logged skip, never a crash;
* a directory agent with the same id overrides the installed one — the
  iterating developer's checkout beats the installed snapshot;
* an explicit ``agents_dir`` argument means "scan exactly this" and does
  not consult entry points unless asked;
* ``LIBRERUN_AGENTS_PATH`` moves the default filesystem root;
* the shipped ``vita_v1`` packaging (pyproject.toml) stays in sync with
  the manifest it installs.

Entry points are faked through the ``registry._agent_entry_points`` seam
— tests must not depend on what happens to be pip-installed in the
running environment.
"""
from __future__ import annotations

import importlib
import sys
import textwrap
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import app.config
from app.agents import registry

BACKEND_DIR = Path(__file__).resolve().parents[1]
VITA_DIR = BACKEND_DIR / "agents" / "vita_v1"


@pytest.fixture(autouse=True)
def _clean_registry():
    registry._clear_registry_for_tests()
    yield
    registry._clear_registry_for_tests()


_TOY_YAML = textwrap.dedent(
    """
    manifest_version: 1
    id: {agent_id}
    name: Toy
    runtime: python-package
    phases:
      - name: analyze
    output:
      mode: html_report
    """
)


def _write_agent_package(
    root: Path,
    name: str,
    *,
    agent_id: str = "toy-ep-v1",
    manifest: bool = True,
) -> Path:
    """Lay out a minimal agent package (``__init__.py`` + ``agent.py`` +
    ``agent.yaml``) under ``root/name`` — same shape for both install
    modes."""
    d = root / name
    d.mkdir(parents=True)
    (d / "__init__.py").write_text("")
    (d / "agent.py").write_text(
        textwrap.dedent(
            f"""
            from app.agents.protocol import AgentProtocol

            class ToyAgent(AgentProtocol):
                agent_id = "{agent_id}"
                display_name = "Toy"
                description = "d"

                def input_schema(self):
                    return {{"type": "object", "properties": {{}}}}
            """
        )
    )
    if manifest:
        (d / "agent.yaml").write_text(_TOY_YAML.format(agent_id=agent_id))
    return d


def _import_package(pkg_dir: Path):
    """Import ``pkg_dir`` as a top-level package, the way a pip-installed
    agent is imported (site-packages is just a directory on sys.path)."""
    parent = str(pkg_dir.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    return importlib.import_module(pkg_dir.name)


def _fake_ep(module=None, *, name: str = "toy", load=None):
    if load is None:
        load = lambda: module  # noqa: E731
    return SimpleNamespace(name=name, value=name, load=load)


# --------------------------- entry-point path --------------------------------


def test_entry_point_agent_registers(tmp_path, monkeypatch):
    pkg = _write_agent_package(tmp_path, f"ep_{tmp_path.name}")
    module = _import_package(pkg)
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [_fake_ep(module)])

    empty_dir = tmp_path / "agents"
    empty_dir.mkdir()
    count = registry.discover_agents(empty_dir, include_entry_points=True)

    assert count == 1
    assert registry.get_agent("toy-ep-v1") is not None
    assert registry.get_manifest("toy-ep-v1").phase_names() == ["analyze"]
    # Assets (scenarios etc.) resolve to the installed package directory.
    assert registry.get_agent_dir("toy-ep-v1") == pkg


def test_entry_point_load_failure_is_a_logged_skip(tmp_path, monkeypatch, caplog):
    def _boom():
        raise ImportError("broken distribution")

    monkeypatch.setattr(
        registry, "_agent_entry_points", lambda: [_fake_ep(load=_boom)]
    )
    empty_dir = tmp_path / "agents"
    empty_dir.mkdir()
    assert registry.discover_agents(empty_dir, include_entry_points=True) == 0
    assert any(
        "agent_entry_point_load_failed" in r.getMessage() for r in caplog.records
    )


def test_entry_point_resolving_to_non_module_is_skipped(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(
        registry, "_agent_entry_points", lambda: [_fake_ep(load=lambda: object())]
    )
    empty_dir = tmp_path / "agents"
    empty_dir.mkdir()
    assert registry.discover_agents(empty_dir, include_entry_points=True) == 0
    assert any(
        "agent_entry_point_not_a_module" in r.getMessage() for r in caplog.records
    )


def test_entry_point_without_manifest_is_skipped(tmp_path, monkeypatch, caplog):
    pkg = _write_agent_package(tmp_path, f"epnm_{tmp_path.name}", manifest=False)
    module = _import_package(pkg)
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [_fake_ep(module)])
    empty_dir = tmp_path / "agents"
    empty_dir.mkdir()
    assert registry.discover_agents(empty_dir, include_entry_points=True) == 0
    assert any("agent_manifest_missing" in r.getMessage() for r in caplog.records)


def test_directory_agent_overrides_installed_one_with_same_id(tmp_path, monkeypatch):
    installed = _write_agent_package(
        tmp_path / "site", f"dup_{tmp_path.name}", agent_id="dup-v1"
    )
    module = _import_package(installed)
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [_fake_ep(module)])

    # The scan root's basename becomes the import package name, so give it
    # a unique identifier-safe name — the real ``agents`` package is
    # already imported by this test process (a fresh chassis boot never
    # pre-imports it; that's the B11 import-isolation invariant).
    agents_dir = tmp_path / f"agdir_{tmp_path.name}"
    checkout = _write_agent_package(agents_dir, "dup", agent_id="dup-v1")

    count = registry.discover_agents(agents_dir, include_entry_points=True)

    # One distinct agent, and the checkout (registered last) wins.
    assert count == 1
    assert registry.get_agent("dup-v1") is not None
    assert registry.get_agent_dir("dup-v1") == checkout
    assert registry.get_agent_dir("dup-v1") != installed


def test_explicit_agents_dir_does_not_consult_entry_points(tmp_path, monkeypatch):
    calls: list[str] = []

    def _spy():
        calls.append("enumerated")
        return []

    monkeypatch.setattr(registry, "_agent_entry_points", _spy)
    empty_dir = tmp_path / "agents"
    empty_dir.mkdir()

    registry.discover_agents(empty_dir)
    assert calls == []

    registry.discover_agents(empty_dir, include_entry_points=True)
    assert calls == ["enumerated"]


# --------------------------- agents-path setting -----------------------------


def test_agents_path_setting_moves_the_default_root(tmp_path, monkeypatch):
    # Unique identifier-safe basename — see the note in the precedence test.
    agents_dir = tmp_path / f"extroot_{tmp_path.name}"
    _write_agent_package(agents_dir, "toy", agent_id="toy-path-v1")

    # Patch the LIVE settings instance: the conftest session fixture
    # replaces ``app.config.settings`` after this module is imported, so a
    # module-level ``from app.config import settings`` binding goes stale.
    monkeypatch.setattr(
        app.config.settings, "LIBRERUN_AGENTS_PATH", str(agents_dir)
    )
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [])

    count = registry.discover_agents()
    assert count == 1
    assert registry.get_agent("toy-path-v1") is not None


def test_blank_agents_path_keeps_the_stable_default(monkeypatch):
    monkeypatch.setattr(app.config.settings, "LIBRERUN_AGENTS_PATH", "")
    assert registry._resolve_agents_dirs() == [registry.AGENTS_DIR]


def test_agents_path_is_a_list_scanned_in_order(tmp_path, monkeypatch):
    """Blueprint S3: ``LIBRERUN_AGENTS_PATH`` takes several directories
    separated by the OS path separator — the demo runs the bundled agent
    and the examples side by side with ``agents:agents/_examples``. Every
    root is scanned; blanks are ignored; a later root's agent replaces an
    earlier one with the same id."""
    import os

    first = tmp_path / f"first_{tmp_path.name}"
    second = tmp_path / f"second_{tmp_path.name}"
    _write_agent_package(first, "one", agent_id="one-v1")
    _write_agent_package(first, "shared", agent_id="shared-v1")
    _write_agent_package(second, "two", agent_id="two-v1")
    _write_agent_package(second, "shared", agent_id="shared-v1")

    monkeypatch.setattr(
        app.config.settings,
        "LIBRERUN_AGENTS_PATH",
        os.pathsep.join([str(first), "", str(second)]),
    )
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [])
    assert registry._resolve_agents_dirs() == [first, second]

    count = registry.discover_agents()
    assert count == 3
    assert {a.agent_id for a in registry.list_agents()} == {"one-v1", "two-v1", "shared-v1"}
    # The second root won the shared id: its directory is the recorded one.
    assert registry.get_agent_dir("shared-v1") == second / "shared"


def test_two_roots_with_the_same_basename_get_distinct_namespaces(tmp_path, monkeypatch):
    """Codex on PR #51: ``/vendor-a/plugins:/vendor-b/plugins`` are two
    portions of ONE namespace package name. Bound to that natural name,
    the second root's ``plugins.shared`` is the first root's cached
    module — the second manifest recorded over the first root's code, or
    (ids differing) an id mismatch and a skip. Each root must import
    under a name that means it and nothing else."""
    import inspect
    import os

    basename = f"plugins_{tmp_path.name}"
    a = tmp_path / "vendor-a" / basename
    b = tmp_path / "vendor-b" / basename
    _write_agent_package(a, "shared", agent_id="shared-a")
    _write_agent_package(b, "shared", agent_id="shared-b")

    monkeypatch.setattr(
        app.config.settings, "LIBRERUN_AGENTS_PATH", os.pathsep.join([str(a), str(b)])
    )
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [])

    assert registry.discover_agents() == 2
    assert {x.agent_id for x in registry.list_agents()} == {"shared-a", "shared-b"}
    for agent_id, root in (("shared-a", a), ("shared-b", b)):
        cls = type(registry.get_agent(agent_id))
        code_file = Path(inspect.getfile(cls)).resolve()
        assert code_file.is_relative_to(root.resolve()), (agent_id, code_file)
        assert registry.get_agent_dir(agent_id) == root / "shared"


def test_custom_root_shadowed_by_the_real_agents_package_still_serves(
    tmp_path, monkeypatch
):
    """Codex P1 (PR #30): a custom root whose basename collides with the
    baked-in regular ``agents`` package must still serve ITS packages.
    Python resolves a regular package over a namespace directory
    regardless of sys.path order, so without the explicit root binding
    this import lands in ``backend/agents`` and external-only agents die
    with ModuleNotFoundError."""
    import agents  # noqa: F401  — bind the real regular package first

    custom_root = tmp_path / "agents"  # colliding basename, no __init__.py
    _write_agent_package(custom_root, "shadow_toy", agent_id="shadow-toy-v1")
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [])

    count = registry.discover_agents(custom_root)

    assert count == 1
    agent = registry.get_agent("shadow-toy-v1")
    assert agent is not None
    module_file = Path(sys.modules[type(agent).__module__].__file__).resolve()
    assert custom_root.resolve() in module_file.parents
    assert registry.get_agent_dir("shadow-toy-v1") == custom_root / "shadow_toy"


def test_non_identifier_root_basename_is_still_importable(tmp_path, monkeypatch):
    """A root like ``/opt/my-agents`` has no valid import name of its own;
    the synthetic parent binding must carry it."""
    custom_root = tmp_path / "my-agents"
    _write_agent_package(custom_root, "dash_toy", agent_id="dash-toy-v1")
    monkeypatch.setattr(registry, "_agent_entry_points", lambda: [])

    count = registry.discover_agents(custom_root)

    assert count == 1
    assert registry.get_agent("dash-toy-v1") is not None


# --------------------------- shipped packaging -------------------------------


def test_vita_pyproject_matches_the_shipped_manifest():
    """Drift guard: the pip install mode must expose the same agent the
    directory mode does — entry-point name == manifest id, module target
    ``vita_v1``, and every non-code asset the contract needs in the
    wheel's package-data."""
    with open(VITA_DIR / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)
    with open(VITA_DIR / "agent.yaml") as f:
        manifest = yaml.safe_load(f)

    eps = pyproject["project"]["entry-points"][registry.ENTRY_POINT_GROUP]
    assert eps == {manifest["id"]: "vita_v1"}
    assert pyproject["project"]["name"] == "vita-agent"

    package_data = pyproject["tool"]["setuptools"]["package-data"]["vita_v1"]
    for asset in ("agent.yaml", "report.html", "scenarios/*.json"):
        assert asset in package_data
    # K5b retired the packaged config.json (D18): the settings are
    # declared in agent.yaml and valued per tenant.
    assert "config.json" not in package_data
