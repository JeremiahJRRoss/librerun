"""The agents a checkout carries, read without the chassis.

Two files are scanned, both with a deliberately narrow reader:

* ``agent.yaml`` — the top-level scalars ``id`` and ``runtime`` and the
  ``url`` under ``container:``, the way ``scripts/demo.sh`` reads ``id``.
  The chassis loader is the authority on a manifest; the CLI never
  validates one, it only needs to know which agents exist and which are
  containers;
* ``agents.compose.yaml`` — the service names and their
  ``librerun.agent_id`` labels, the way ``compose.sh`` reads them, because
  an id alone cannot name a service (the echo example's manifest id is
  ``echo-v1`` and its service ``echo-agent``).

No YAML library: the CLI is standard-library only (D5), and these two
readers cover exactly the shapes the shipped files and the templates
use. A manifest laid out differently — the id in a flow mapping, say —
is still a valid manifest to the chassis and simply invisible here,
which every command says when it cannot find an agent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Where the demo puts agents: LIBRERUN_AGENTS_PATH=agents:agents/_examples,
# relative to backend/. ``scripts/demo.sh`` scans the same two roots to
# provision keys.
AGENT_ROOTS = ("agents", "agents/_examples")

_SCALAR = re.compile(r"""^(?P<key>[A-Za-z_][A-Za-z0-9_]*):\s*(?P<value>.*?)\s*$""")
_QUOTED = re.compile(r"""^(?:"([^"]*)"|'([^']*)')\s*(?:#.*)?$""")


@dataclass
class ManifestSummary:
    directory: Path
    id: str
    runtime: str
    container_url: str | None = None

    @property
    def relative_to_backend(self) -> str:
        """``agents/my_agent`` — the path inside the backend image."""
        parts = self.directory.parts
        idx = len(parts) - 1 - parts[::-1].index("backend")
        return "/".join(parts[idx + 1 :])


def _unquote(value: str) -> str:
    value = value.strip()
    m = _QUOTED.match(value)
    if m:
        return m.group(1) if m.group(1) is not None else m.group(2)
    return value.split(" #", 1)[0].strip()


def read_manifest(agent_dir: Path) -> ManifestSummary | None:
    """The summary, or None when the file is absent or carries no ``id``."""
    path = agent_dir / "agent.yaml"
    if not path.is_file():
        return None
    values: dict[str, str] = {}
    container_url = None
    in_container = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indented = line[0] in " \t"
        if not indented:
            m = _SCALAR.match(line)
            if not m:
                in_container = False
                continue
            key, value = m.group("key"), m.group("value")
            in_container = key == "container"
            if value and not value.startswith("#"):
                values[key] = _unquote(value)
            continue
        if in_container:
            m = _SCALAR.match(line.strip())
            if m and m.group("key") == "url":
                container_url = _unquote(m.group("value"))
    agent_id = values.get("id")
    if not agent_id:
        return None
    return ManifestSummary(
        directory=agent_dir,
        id=agent_id,
        runtime=values.get("runtime", "python-package"),
        container_url=container_url,
    )


def scan_agents(root: Path, roots=AGENT_ROOTS) -> list[ManifestSummary]:
    """Every agent directory under the demo's roots, in scan order.
    Directories starting with ``_`` are skipped, as discovery skips them."""
    found: list[ManifestSummary] = []
    for rel in roots:
        base = root / "backend" / rel
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if not entry.is_dir() or entry.name.startswith(("_", ".")):
                continue
            summary = read_manifest(entry)
            if summary is not None:
                found.append(summary)
    return found


def find_agent(root: Path, agent_id: str) -> ManifestSummary | None:
    for summary in scan_agents(root):
        if summary.id == agent_id:
            return summary
    return None


@dataclass
class FragmentService:
    name: str
    agent_id: str | None = None
    container_name: str | None = None
    profiles: list[str] = field(default_factory=list)


_SERVICE_HEADER = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
_LABEL = re.compile(r"^\s+librerun\.agent_id:\s*(.+?)\s*$")
_CONTAINER_NAME = re.compile(r"^\s+container_name:\s*(.+?)\s*$")
_PROFILES_INLINE = re.compile(r"^\s+profiles:\s*\[(.*)\]\s*$")
_PROFILES_BLOCK = re.compile(r"^\s+profiles:\s*$")
_LIST_ITEM = re.compile(r"^\s+-\s*(.+?)\s*$")


def read_fragment(root: Path) -> list[FragmentService]:
    """The services of ``agents.compose.yaml`` with their labels — the
    same line shapes ``compose.sh``'s reader recognises."""
    path = root / "agents.compose.yaml"
    if not path.is_file():
        return []
    services: list[FragmentService] = []
    current: FragmentService | None = None
    in_services = False
    in_profiles = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            in_services = line.startswith("services:")
            current = None
            continue
        if not in_services:
            continue
        m = _SERVICE_HEADER.match(line)
        if m:
            current = FragmentService(name=m.group(1))
            services.append(current)
            in_profiles = False
            continue
        if current is None:
            continue
        m = _LABEL.match(line)
        if m:
            current.agent_id = _unquote(m.group(1))
            in_profiles = False
            continue
        m = _CONTAINER_NAME.match(line)
        if m:
            current.container_name = _unquote(m.group(1))
            in_profiles = False
            continue
        m = _PROFILES_INLINE.match(line)
        if m:
            current.profiles = [
                _unquote(p) for p in m.group(1).split(",") if _unquote(p)
            ]
            in_profiles = False
            continue
        if _PROFILES_BLOCK.match(line):
            in_profiles = True
            continue
        if in_profiles:
            m = _LIST_ITEM.match(line)
            if m:
                current.profiles.append(_unquote(m.group(1)))
                continue
            in_profiles = False
    return services


def service_for(root: Path, agent_id: str) -> FragmentService | None:
    for service in read_fragment(root):
        if service.agent_id == agent_id:
            return service
    return None
