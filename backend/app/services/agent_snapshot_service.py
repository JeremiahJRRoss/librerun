"""Manifest snapshots: what the gateway reads instead of the registry.

The LLM gateway runs in its own process (blueprint S4a, L23), so it
cannot ask ``app.agents.registry`` anything — that dict lives here and
dies with this process. Discovery is the one place every agent passes
through, whatever its install mode, so discovery is where the validated
manifest becomes a row.

Reconciliation is a whole pass, not a per-agent write:

* every registered agent is **upserted** — manifest, its sha256, the
  install mode it came from, ``discovered_at`` now, and ``absent_at``
  cleared, so an agent that comes back is present again;
* every row the pass did **not** touch is stamped ``absent_at`` — an
  uninstalled agent, or one whose manifest stopped validating and which
  discovery therefore skipped. The gateway treats such a row as no agent
  at all (``403 agent_unknown``).

Rows are never deleted. ``agent_keys`` and ``agent_step_configs`` name
an agent by id and keep their context across an uninstall, so removing
the snapshot would quietly throw away a tenant's model choices and an
operator's issued key the first time an agent directory was moved.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@dataclass
class ReconcileReport:
    """What one pass did — logged at boot, and asserted by the tests."""

    present: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    marked_absent: list[str] = field(default_factory=list)
    returned: list[str] = field(default_factory=list)


def manifest_payload(manifest) -> dict:
    """The validated manifest as JSON — defaults filled in, aliases
    normalised. The gateway reads this, not the YAML: what the chassis
    accepted is what authorizes the call."""
    return manifest.model_dump(mode="json")


def manifest_sha256(payload: dict) -> str:
    """A stable digest of the snapshot. Canonical JSON (sorted keys,
    tight separators) so an unchanged manifest keeps its digest across
    processes and Python versions."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_UPSERT = text(
    """
    INSERT INTO agent_manifests
        (agent_id, manifest, sha256, source, discovered_at, absent_at)
    VALUES (:agent_id, CAST(:manifest AS JSONB), :sha256, :source, NOW(), NULL)
    ON CONFLICT (agent_id) DO UPDATE SET
        manifest = EXCLUDED.manifest,
        sha256 = EXCLUDED.sha256,
        source = EXCLUDED.source,
        discovered_at = NOW(),
        absent_at = NULL
    RETURNING (xmax = 0) AS inserted
    """
)

# ``<> ALL(:present)`` is true for every row when the array is empty, so
# a deployment with zero agents on disk stamps them all — which is the
# honest answer, not a special case to skip.
_MARK_ABSENT = text(
    """
    UPDATE agent_manifests
       SET absent_at = NOW()
     WHERE absent_at IS NULL
       AND agent_id <> ALL(:present)
    RETURNING agent_id
    """
)


async def reconcile_snapshots(
    db: AsyncSession, entries: list[tuple[str, object, str]]
) -> ReconcileReport:
    """Upsert a snapshot per registered agent, then stamp the rest absent.

    ``entries`` is ``(agent_id, manifest, source)`` — what
    ``registry.registered_manifests()`` returns. The caller owns the
    transaction.
    """
    report = ReconcileReport()

    # Read the prior state first: which rows exist, their digest, and
    # whether they were absent. That is the only way to say "this agent
    # came back" or "its manifest changed" rather than re-announcing
    # every agent on every boot.
    before = {
        row.agent_id: (row.sha256, row.absent_at)
        for row in (
            await db.execute(text("SELECT agent_id, sha256, absent_at FROM agent_manifests"))
        ).all()
    }

    for agent_id, manifest, source in entries:
        payload = manifest_payload(manifest)
        digest = manifest_sha256(payload)
        await db.execute(
            _UPSERT,
            {
                "agent_id": agent_id,
                "manifest": json.dumps(payload),
                "sha256": digest,
                "source": source[:32],
            },
        )
        report.present.append(agent_id)
        prior = before.get(agent_id)
        if prior is None or prior[0] != digest:
            report.changed.append(agent_id)
        if prior is not None and prior[1] is not None:
            report.returned.append(agent_id)

    result = await db.execute(_MARK_ABSENT, {"present": report.present})
    report.marked_absent = [row.agent_id for row in result.all()]
    return report


async def reconcile_registered_agents(db: AsyncSession) -> ReconcileReport:
    """Reconcile from the live registry — what boot calls."""
    from app.agents.registry import registered_manifests

    report = await reconcile_snapshots(db, registered_manifests())
    logger.info(
        "agent_snapshots_reconciled",
        present=report.present,
        changed=report.changed,
        returned=report.returned,
        marked_absent=report.marked_absent,
    )
    return report
