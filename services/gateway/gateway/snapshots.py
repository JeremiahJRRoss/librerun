"""Reading ``agent_manifests`` — the gateway's authority on an agent.

The registry the backend fills at discovery is process-local, so this is
what the gateway asks instead. A row stamped ``absent_at`` is **no
agent**: its directory is gone, or its manifest stopped validating, and
either way nothing it once declared should still authorize a call.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class Snapshot:
    agent_id: str
    manifest: dict

    @property
    def grants(self) -> list[str]:
        return list(self.manifest.get("capabilities") or [])

    def grants_capability(self, name: str) -> bool:
        return name in self.grants

    @property
    def llm(self) -> dict:
        value = self.manifest.get("llm")
        return value if isinstance(value, dict) else {}

    @property
    def steps(self) -> list[dict]:
        return [s for s in (self.llm.get("steps") or []) if isinstance(s, dict)]

    def step(self, step_id: str) -> dict | None:
        for s in self.steps:
            if s.get("id") == step_id:
                return s
        return None

    @property
    def redact_outbound(self) -> bool:
        # Absent means on. A snapshot written before the field existed, or
        # one whose llm block is missing entirely, must not read as an
        # opt-out — the safe value is the one you get by saying nothing.
        return bool(self.llm.get("redact_outbound", True))


_SELECT = text(
    """
    SELECT agent_id, manifest
      FROM agent_manifests
     WHERE agent_id = :agent_id
       AND absent_at IS NULL
    """
)


async def load(db: AsyncSession, agent_id: str) -> Snapshot | None:
    row = (await db.execute(_SELECT, {"agent_id": agent_id})).first()
    if row is None:
        return None
    manifest = row.manifest if isinstance(row.manifest, dict) else {}
    return Snapshot(agent_id=row.agent_id, manifest=manifest)
