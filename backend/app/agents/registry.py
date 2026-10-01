"""Agent registry and discovery (filesystem + pip entry points).

Agents arrive two ways (blueprint B12):

1. **Directory install** — self-contained packages under the agents
   directory (``LIBRERUN_AGENTS_PATH``, default the ``agents/`` sibling
   of ``app/``). At startup the shell walks that directory.
2. **Pip install** — a distribution that declares a ``librerun.agents``
   entry point resolving to its agent package (e.g. a ``my-agent``
   distribution exposing ``my_agent``). No agents-directory presence
   needed.

Either way the contract is identical: the package directory ships an
``agent.yaml`` manifest (blueprint B7 — required), the package exports an
``AgentProtocol`` subclass, and discovery instantiates + registers one
instance alongside its parsed manifest. The manifest is what the chassis
consults for the agent's phase list, output mode, feedback sections,
scenarios directory, and UI hints. When both modes provide the same
agent id, the directory copy wins (registered last) — the iterating
developer's checkout beats the installed snapshot.

The registry is intentionally a module-level dict rather than an injected
service — agents are process-global singletons once the app has started,
and every caller (routers, background tasks, tests) looks them up by id.
"""
from __future__ import annotations

import hashlib
import importlib
import os
import importlib.util
import inspect
import sys
from pathlib import Path
from types import ModuleType

import structlog

from app.agents.manifest import (
    AgentManifest,
    ManifestError,
    default_manifest,
    load_manifest,
)
from app.agents.protocol import AgentProtocol

logger = structlog.get_logger(__name__)

# Directory on disk where agent packages live. Resolved relative to this
# file so the path is stable regardless of cwd: ``backend/app/agents/registry.py``
# → parents[2] == ``backend/`` → ``backend/agents``.
AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"

_registry: dict[str, AgentProtocol] = {}
_manifests: dict[str, AgentManifest] = {}
_agent_dirs: dict[str, Path] = {}
# How each agent got here. Read by the manifest-snapshot reconciliation
# (blueprint S4a), which the gateway queries instead of this registry —
# a separate service cannot import a dict that lives in this process.
_sources: dict[str, str] = {}


def register(
    agent: AgentProtocol,
    manifest: AgentManifest | None = None,
    agent_dir: Path | None = None,
    source: str = "direct",
) -> None:
    """Add ``agent`` to the registry keyed by its ``agent_id``.

    ``manifest`` is stored alongside the instance; direct callers that
    don't pass one (test stubs, embedded fixtures) get the back-compat
    ``default_manifest`` — the pre-B7 analyze/investigate shape. On-disk
    agents always come through ``discover_agents``, which requires a real
    ``agent.yaml``.

    A duplicate id logs a warning and overwrites the previous entry rather
    than raising — tests rely on being able to register-then-replace stubs,
    and in production discovery only runs once per process so collisions
    are an honest misconfiguration worth surfacing but not crashing on.
    """
    existing = _registry.get(agent.agent_id)
    if existing is not None and existing is not agent:
        logger.warning(
            "agent_register_duplicate",
            agent_id=agent.agent_id,
            replaced=type(existing).__name__,
            with_=type(agent).__name__,
        )
    _registry[agent.agent_id] = agent
    _manifests[agent.agent_id] = manifest or default_manifest(agent)
    _sources[agent.agent_id] = source
    if agent_dir is not None:
        _agent_dirs[agent.agent_id] = agent_dir
    else:
        _agent_dirs.pop(agent.agent_id, None)


def get_agent(agent_id: str) -> AgentProtocol | None:
    return _registry.get(agent_id)


def get_manifest(agent_id: str) -> AgentManifest | None:
    return _manifests.get(agent_id)


def get_agent_dir(agent_id: str) -> Path | None:
    """On-disk directory of a discovered agent (None for direct/test
    registrations) — where its scenarios and other assets live."""
    return _agent_dirs.get(agent_id)


def list_agents() -> list[AgentProtocol]:
    return list(_registry.values())


def get_source(agent_id: str) -> str:
    """How this agent was registered: ``directory``, ``entry_point``, or
    ``direct`` for a caller that bypassed discovery (test stubs, embedded
    fixtures)."""
    return _sources.get(agent_id, "direct")


def registered_manifests() -> list[tuple[str, AgentManifest, str]]:
    """``(agent_id, manifest, source)`` for every registered agent, in
    registration order — what the manifest-snapshot reconciliation writes
    to ``agent_manifests`` so the gateway can read it (blueprint S4a)."""
    return [
        (agent_id, _manifests[agent_id], get_source(agent_id))
        for agent_id in _registry
        if agent_id in _manifests
    ]


def _clear_registry_for_tests() -> None:
    """Test-only hook: empty the registry between tests.

    Production code never calls this. Exposed as a module-level function
    rather than touching ``_registry`` directly so tests don't depend on
    the private name.
    """
    _registry.clear()
    _manifests.clear()
    _agent_dirs.clear()
    _sources.clear()


def _find_agent_class(module) -> type[AgentProtocol] | None:
    """Return the first ``AgentProtocol`` subclass defined in ``module``.

    Walks the module's top-level attributes; ignores the base class itself
    and anything imported from elsewhere (``obj.__module__`` must match).
    """
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj is AgentProtocol:
            continue
        if not issubclass(obj, AgentProtocol):
            continue
        if obj.__module__ != module.__name__ and not obj.__module__.startswith(
            module.__name__ + "."
        ):
            continue
        return obj
    return None


# Entry-point group installed agent packages register under (blueprint
# B12): a distribution declares ``[project.entry-points."librerun.agents"]
# <name> = "<package>"`` and the chassis discovers it at startup with no
# agents-directory presence at all.
ENTRY_POINT_GROUP = "librerun.agents"


def _agent_entry_points() -> list:
    """Enumerate installed ``librerun.agents`` entry points.

    A seam by design: tests (and the zero-agents boot subprocess) replace
    this to simulate 'nothing installed' without uninstalling anything.
    Enumeration failure is logged and treated as an empty set — a broken
    distribution's metadata must never crash the server.
    """
    import importlib.metadata

    try:
        return list(importlib.metadata.entry_points(group=ENTRY_POINT_GROUP))
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "agent_entry_point_enumeration_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return []


def _resolve_agents_dirs() -> list[Path]:
    """Effective filesystem discovery roots, in scan order (blueprint B12;
    a list since S3).

    ``LIBRERUN_AGENTS_PATH`` wins when set: one directory, or several
    separated by ``os.pathsep`` (``agents:agents/_examples`` is the demo's
    value), each absolute or relative to the process working directory,
    blanks ignored. The default is the ``agents/`` directory next to
    ``app/`` — ``./agents`` in the standard ``cd backend`` layout, resolved
    from this file so a different cwd doesn't move it. A later directory's
    agent replaces an earlier one with the same id (``register`` warns).
    """
    import os

    try:
        from app.config import settings

        configured = settings.LIBRERUN_AGENTS_PATH
    except Exception:  # pragma: no cover — config must never block discovery
        configured = ""
    dirs = [
        Path(part.strip()).expanduser()
        for part in (configured or "").split(os.pathsep)
        if part.strip()
    ]
    return dirs or [AGENTS_DIR]


def _load_gated_manifest(agent_dir: Path):
    """Load ``agent_dir``'s manifest for discovery. Returns the manifest,
    or None (logged) when it is missing or invalid."""
    if not (agent_dir / "agent.yaml").exists():
        logger.warning(
            "agent_manifest_missing",
            path=str(agent_dir),
            hint="every agent ships an agent.yaml from B7 on — "
            "`librerun init <name> --template langgraph|container-python|"
            "container-ts` scaffolds one (docs/authoring/Manifest.md is the "
            "field reference)",
        )
        return None
    try:
        return load_manifest(agent_dir)
    except ManifestError as exc:
        logger.error("agent_manifest_invalid", path=str(agent_dir), error=str(exc))
        return None


_ENV_REF_RE = None  # compiled lazily below


def _expand_env_refs(value: str) -> str | None:
    """Expand ``${VAR}`` references from the chassis environment.

    Returns None (and logs nothing — the caller logs with context) when a
    referenced variable is unset or blank: an unaddressable container
    agent must not register.
    """
    global _ENV_REF_RE
    import os
    import re

    if _ENV_REF_RE is None:
        _ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

    unresolved: list[str] = []

    def _sub(m: "re.Match[str]") -> str:
        got = os.environ.get(m.group(1), "")
        if not got:
            unresolved.append(m.group(1))
        return got

    expanded = _ENV_REF_RE.sub(_sub, value)
    if unresolved:
        return None
    return expanded


def _register_container_agent(entry: Path, manifest) -> str | None:
    """Register one ``runtime: container`` agent (blueprint B12a).

    No import happens — the registry entry is a ``ContainerAgent`` proxy
    that speaks Run Contract v1 to ``container.url``. The manifest
    validator already guaranteed ``container`` and ``input_schema`` are
    present; here we resolve the URL's env references and check the
    schema file actually shipped.
    """
    from app.agents.container import ContainerAgent

    url = _expand_env_refs(manifest.container.url)
    if not url:
        logger.warning(
            "agent_container_url_unresolved",
            path=str(entry),
            agent_id=manifest.id,
            url=manifest.container.url,
            hint="set the referenced environment variable(s) so the "
            "chassis can address the container, or hardcode the url",
        )
        return None

    schema_path = entry / manifest.input_schema
    if not schema_path.is_file():
        logger.warning(
            "agent_container_schema_missing",
            path=str(entry),
            agent_id=manifest.id,
            input_schema=manifest.input_schema,
            hint="container agents must ship the JSON Schema file the "
            "manifest's input_schema names",
        )
        return None

    instance = ContainerAgent(manifest, url, entry)
    register(instance, manifest, agent_dir=entry, source="directory")
    logger.info(
        "agent_registered",
        agent_id=manifest.id,
        module=None,
        source="directory",
        display_name=manifest.name,
        phases=manifest.phase_names(),
        runtime=manifest.runtime,
        container_url=url,
    )
    return manifest.id


def _instantiate_and_register(module, manifest, agent_dir: Path, source: str) -> str | None:
    """Shared tail of both discovery paths: find the ``AgentProtocol``
    class (preferring an ``agent`` submodule, as the template lays out),
    instantiate it, enforce manifest identity, and register.

    Returns the registered agent id, or None (logged) on any refusal.
    """
    module_name = module.__name__
    # Prefer an ``agent`` submodule if present — the template uses
    # ``agent.py`` to hold the class — otherwise use the package
    # itself (the package __init__ may re-export the class).
    agent_module = module
    if (agent_dir / "agent.py").exists():
        agent_module = importlib.import_module(f"{module_name}.agent")

    agent_cls = _find_agent_class(agent_module) or _find_agent_class(module)
    if agent_cls is None:
        logger.warning(
            "agent_discovery_no_class",
            module=module_name,
        )
        return None

    instance = agent_cls()

    # The manifest is the registry key's source of truth; a class
    # whose ``agent_id`` disagrees would be reachable under one id
    # and traced/persisted under another, so refuse to register it.
    if instance.agent_id != manifest.id:
        logger.error(
            "agent_manifest_id_mismatch",
            module=module_name,
            manifest_id=manifest.id,
            class_agent_id=instance.agent_id,
        )
        return None
    if getattr(instance, "display_name", None) != manifest.name:
        # Cosmetic drift — surface it, manifest wins on API output.
        logger.warning(
            "agent_manifest_name_mismatch",
            agent_id=manifest.id,
            manifest_name=manifest.name,
            class_display_name=getattr(instance, "display_name", None),
        )

    register(instance, manifest, agent_dir=agent_dir, source=source)
    logger.info(
        "agent_registered",
        agent_id=instance.agent_id,
        module=module_name,
        source=source,
        display_name=getattr(instance, "display_name", None),
        phases=manifest.phase_names(),
        runtime=manifest.runtime,
    )
    return instance.agent_id


def _register_from_entry_point(ep) -> str | None:
    """Register one installed agent from its ``librerun.agents`` entry
    point. The entry point must resolve to a package (or module with an
    on-disk location) whose directory ships the same ``agent.yaml`` +
    assets contract as a directory-installed agent. Any failure is logged
    and skipped — one broken installed agent must not prevent the others
    from loading, and must never crash the server."""
    try:
        loaded = ep.load()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "agent_entry_point_load_failed",
            entry_point=f"{ep.name} = {ep.value}",
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return None

    if not isinstance(loaded, ModuleType):
        logger.error(
            "agent_entry_point_not_a_module",
            entry_point=f"{ep.name} = {ep.value}",
            resolved_type=type(loaded).__name__,
            hint="point the entry point at the agent package itself, "
            'e.g. my-agent = "my_agent"',
        )
        return None

    module_file = getattr(loaded, "__file__", None)
    if not module_file:
        logger.error(
            "agent_entry_point_no_location",
            entry_point=f"{ep.name} = {ep.value}",
            hint="namespace packages can't carry agent.yaml — ship the "
            "agent as a regular package",
        )
        return None
    agent_dir = Path(module_file).resolve().parent

    manifest = _load_gated_manifest(agent_dir)
    if manifest is None:
        return None
    if manifest.runtime != "python-package":
        # An entry point IS a python package — a container manifest riding
        # one is a contradiction. Container agents register from the
        # agents directory (blueprint B12a).
        logger.warning(
            "agent_runtime_unsupported",
            path=str(agent_dir),
            agent_id=manifest.id,
            runtime=manifest.runtime,
            hint="container agents register from the agents directory "
            "(their manifest names a url, not code) — entry points are "
            "for python-package agents",
        )
        return None
    try:
        return _instantiate_and_register(loaded, manifest, agent_dir, source="entry-point")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "agent_discovery_failed",
            module=loaded.__name__,
            error=str(exc),
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return None


# Import-parent bindings for scanned agents directories, keyed by resolved
# path — reused across repeated discovery calls so a directory always maps
# to one package name per process.
_BOUND_ROOTS: dict[Path, str] = {}


def _root_import_name(directory: Path) -> str:
    """The parent package name agent packages in ``directory`` import under.

    The natural choice is the directory's own basename (in-tree:
    ``agents``) — but Python's import system may resolve that name
    somewhere else entirely: a regular package with the same name
    anywhere on ``sys.path`` (the baked-in ``backend/agents``) beats a
    namespace directory *regardless of path order*, so a custom
    ``LIBRERUN_AGENTS_PATH`` named ``agents`` would silently import the
    baked-in copies (or fail on external-only agents). And a namespace
    package is one name for every same-named directory on ``sys.path``:
    with two roots ``/vendor-a/plugins`` and ``/vendor-b/plugins``
    (blueprint S3's path list) the second root's ``plugins.shared`` is
    the first root's cached module — its manifest recorded over the
    other's code, or an id mismatch and a skip (Codex on PR #51). Use
    the natural name only when it provably means ``directory`` and
    nothing else, and no other root holds it; otherwise bind a synthetic
    parent package directly to the directory so imports cannot land
    anywhere else.
    """
    directory = directory.resolve()
    cached = _BOUND_ROOTS.get(directory)
    if cached is not None:
        return cached

    name = directory.name
    resolved: str | None = None
    claimed_elsewhere = any(
        bound == name and other != directory for other, bound in _BOUND_ROOTS.items()
    )
    if name.isidentifier() and not claimed_elsewhere:
        existing = sys.modules.get(name)
        if existing is not None:
            locations = list(getattr(existing, "__path__", []))
        else:
            try:
                spec = importlib.util.find_spec(name)
            except (ImportError, ValueError):
                spec = None
            locations = list(getattr(spec, "submodule_search_locations", None) or [])
        # Exactly this directory, not "among others": a namespace path
        # that spans several roots would share child modules between them.
        if [Path(p).resolve() for p in locations] == [directory]:
            resolved = name

    if resolved is None:
        digest = hashlib.sha1(str(directory).encode()).hexdigest()[:8]
        resolved = f"librerun_agents_{digest}"
        if resolved not in sys.modules:
            pkg = ModuleType(resolved)
            pkg.__path__ = [str(directory)]  # type: ignore[attr-defined]
            sys.modules[resolved] = pkg
        logger.info(
            "agent_root_bound",
            path=str(directory),
            package=resolved,
            reason="basename_shadowed_shared_or_not_importable",
        )

    _BOUND_ROOTS[directory] = resolved
    return resolved


def discover_agents(
    agents_dir: Path | None = None,
    include_entry_points: bool | None = None,
) -> int:
    """Discover and register agents from both install modes (blueprint B12).

    Installed (pip entry-point) agents register first, then the agents
    directory is scanned — so a checked-out agent with the same id
    overrides an installed one, which is what an iterating developer
    wants. Directories that begin with ``_`` (e.g. ``_examples``) are
    skipped — a root to be named on ``LIBRERUN_AGENTS_PATH``, not an
    agent. Every agent must
    ship an ``agent.yaml`` manifest (blueprint B7); a missing or invalid
    manifest, an unsupported runtime, or an import failure is logged and
    that agent skipped — one bad agent must not prevent the others from
    loading, and must never crash the server.

    ``include_entry_points`` defaults to True for the bare production
    call and False when ``agents_dir`` is passed explicitly — an explicit
    directory means "scan exactly this" (the contract every existing
    caller and test relies on); pass ``include_entry_points=True`` to
    combine.

    Returns the number of distinct agents registered by this call so the
    caller can log a single roll-up event.
    """
    if include_entry_points is None:
        include_entry_points = agents_dir is None

    registered: set[str] = set()

    if include_entry_points:
        for ep in _agent_entry_points():
            agent_id = _register_from_entry_point(ep)
            if agent_id is not None:
                registered.add(agent_id)

    directories = [agents_dir] if agents_dir is not None else _resolve_agents_dirs()
    for directory in directories:
        _discover_directory(directory, registered)

    logger.info(
        "agent_discovery_complete",
        count=len(registered),
        path=os.pathsep.join(str(d) for d in directories),
        entry_points=include_entry_points,
    )
    return len(registered)


def _discover_directory(directory: Path, registered: set[str]) -> None:
    """Scan one discovery root, adding every registered id to ``registered``."""
    if not directory.exists():
        logger.info("agent_discovery_skipped", reason="directory_missing", path=str(directory))
        return
    if not directory.is_dir():
        logger.warning("agent_discovery_skipped", reason="not_a_directory", path=str(directory))
        return

    # Make the parent of the agents directory importable so the natural
    # ``import <dirname>.<agent>`` form can resolve. In the standard
    # uvicorn layout (``cd backend && uvicorn app.main:app``) this is a
    # no-op because ``backend/`` is already on sys.path.
    parent = str(directory.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    # ``agents`` in-tree; a synthetic bound package when the basename
    # would resolve elsewhere (see _root_import_name).
    pkg_name = _root_import_name(directory)
    for entry in sorted(directory.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("_") or entry.name.startswith("."):
            continue
        has_python_markers = (entry / "__init__.py").exists() or (
            entry / "agent.py"
        ).exists()
        if not has_python_markers and not (entry / "agent.yaml").exists():
            # Neither code nor manifest — not an agent directory at all.
            continue

        manifest = _load_gated_manifest(entry)
        if manifest is None:
            continue

        if manifest.runtime == "container":
            # No import — the registry entry is a Run Contract v1 proxy
            # (blueprint B12a).
            agent_id = _register_container_agent(entry, manifest)
            if agent_id is not None:
                registered.add(agent_id)
            continue

        if not has_python_markers:
            # A python-package manifest with no python package — same
            # silent skip as before B12a (the directory was never
            # importable), the manifest alone doesn't change that.
            continue

        module_name = f"{pkg_name}.{entry.name}"
        try:
            module = importlib.import_module(module_name)
            agent_id = _instantiate_and_register(module, manifest, entry, source="directory")
            if agent_id is not None:
                registered.add(agent_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "agent_discovery_failed",
                module=module_name,
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
