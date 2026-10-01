"""An agent's tool secrets, as the admin API describes them (K8a; L31, D32).

Never a value. Each declared name has two rows — this tenant's and every
tenant's default — and, for an in-process agent, the backend's environment
as the fallback; each says whether it is set, its fingerprint, who set it
and when, and when a run last read it. A value goes in by ``PUT`` and
never comes back out.
"""
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class SecretRow(BaseModel):
    """One place a value may come from. ``fingerprint`` is a keyed,
    12-character digest under the store key, ``None`` while unset and for a
    row no configured key opens; ``updated_by`` of the default row is shown
    to platform admins alone. The environment row carries ``set`` alone:
    the platform neither fingerprints nor stamps it."""

    set: bool
    fingerprint: str | None = None
    updated_at: datetime | None = None
    updated_by: UUID | None = None
    last_used_at: datetime | None = None


class AgentSecretState(BaseModel):
    """One declared name in this tenant. ``effective`` is where a run of
    this tenant would read it now: ``tenant``, ``agent`` (the default),
    ``environment`` (an in-process agent's fallback) or ``unset``.
    ``environment`` is ``null`` for a container, whose own environment the
    platform cannot see."""

    name: str
    effective: Literal["tenant", "agent", "environment", "unset"]
    tenant: SecretRow
    agent: SecretRow
    environment: SecretRow | None = None


class AgentSecretsList(BaseModel):
    """``GET /agents/{id}/secrets``: every name the manifest declares, in
    its order."""

    agent_id: str
    runtime: str
    secrets: list[AgentSecretState]
