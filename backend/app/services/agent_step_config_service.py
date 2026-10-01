"""A tenant's per-step model configuration (blueprint S4a, L25).

Until this batch the admin route called ``agent.update_step_configs()``
on the process-global agent instance, and the one implementation of that
wrote one shared file. Two tenants editing the same step meant the
second silently overwrote the first, and every tenant read the other's
choice. This table is the fix: a row is keyed by ``(tenant_id,
agent_id, step_id)``, and the manifest's ``llm.steps`` defaults are the
fallback when no row exists.

A row never *declares* a step. It replaces values for one the manifest
still declares, so a row left behind by a step that was removed or
renamed is inert — which is also what the gateway does with it
(``services/gateway/gateway/steps.py``).
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AgentStepConfig

# What an admin may set. The step's id and label are the manifest's, and
# an override that could rename a step would let a tenant's edit change
# what the agent declares it calls.
EDITABLE_FIELDS = ("provider", "model", "temperature", "max_tokens", "timeout_seconds")


class InvalidStepOverride(ValueError):
    """An override the manifest's own schema would not accept."""

    def __init__(self, step_id: str, detail: str):
        super().__init__(f"step {step_id!r}: {detail}")
        self.step_id = step_id
        self.detail = detail


def validate_override(step_id: str, values: dict) -> None:
    """Hold an override to the rules a DECLARATION already obeys.

    Not a second copy of them: the values go through ``LlmStepSpec``,
    the manifest's own model, so the two cannot drift. Without this an
    admin could store `max_tokens: 0` or a negative temperature — the
    row is written, the gateway gives it precedence over the manifest,
    and every invocation of that step fails until somebody finds the row
    (Codex P2). An unusable value belongs in a 400, not in the database.

    The rules are exactly the manifest's, no stricter: there is no
    provider allow-list here because there is none there either, and an
    override that could be refused for a provider a declaration may name
    would be a second, invisible vocabulary.
    """
    from pydantic import ValidationError

    from app.agents.manifest import LlmStepSpec

    try:
        LlmStepSpec(id=step_id, **values)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first.get("loc") or ()) or "value"
        raise InvalidStepOverride(step_id, f"{field}: {first.get('msg')}") from exc


def _is_default(value, default) -> bool:
    """Whether a submitted value says the same thing as the declaration.

    Plain ``==``, deliberately: by the time this runs the value has been
    through ``validate_override`` and is of the type the declaration's
    own model accepts, so the cross-type cases that would need care here
    — a boolean against a number, a string against an int — are already
    a 400 and cannot reach it. What ``==`` does have to get right is the
    one that DOES arrive: a number input parses ``0`` where the manifest
    declares ``0.0``, and those are the same value.
    """
    if value is None or default is None:
        return value is None and default is None
    return value == default


async def overrides_for(
    db: AsyncSession, tenant_id: UUID, agent_id: str
) -> dict[str, dict]:
    rows = (
        await db.execute(
            select(AgentStepConfig).where(
                AgentStepConfig.tenant_id == tenant_id,
                AgentStepConfig.agent_id == agent_id,
            )
        )
    ).scalars()
    return {
        row.step_id: {
            field: getattr(row, field)
            for field in EDITABLE_FIELDS
            if getattr(row, field) is not None
        }
        for row in rows
    }


def effective_steps(manifest, overrides: dict[str, dict]) -> list[dict]:
    """The steps this tenant's calls will actually use.

    Declared by the manifest, in the manifest's order; each field
    replaced by the tenant's row where it has one. ``overridden`` names
    the fields the tenant changed, so the admin page can show what is a
    local choice and what is the agent's default.
    """
    out = []
    for step in manifest.llm.steps if manifest is not None else []:
        override = overrides.get(step.id) or {}
        entry = {
            "step_id": step.id,
            "label": step.label,
            "description": step.description,
        }
        for field in EDITABLE_FIELDS:
            entry[field] = override.get(field, getattr(step, field))
        entry["overridden"] = sorted(override)
        out.append(entry)
    return out


async def apply_updates(
    db: AsyncSession,
    tenant_id: UUID,
    agent_id: str,
    manifest,
    updates: list[dict],
    user_id: UUID | None,
) -> list[str]:
    """Write a tenant's edits. Returns the step ids actually changed.

    An update naming a step the manifest does not declare is ignored
    rather than stored: storing it would leave a row that can never take
    effect and would read, on the admin page, like a setting that does
    nothing. A field set back to ``null`` clears the override and returns
    that field to the manifest's default — and so does a field sent with
    the manifest's own value, which is the same statement.

    That last rule is what keeps this table small and honest. An editor
    posts a whole row, defaults and all — it has no way to know which of
    the values it is showing the tenant ever chose. Stored literally,
    one click of Save turns every default into a tenant override, the
    page says ``overridden here`` about every field, and the agent's
    next release can never reach that tenant again: the author ships a
    new default model and the row keeps pinning the old one, with
    nothing on the page to say why (Codex P2). A row exists to record a
    DIVERGENCE, so a value identical to the default is not one.
    """
    declared = {
        s.id: s for s in (manifest.llm.steps if manifest is not None else [])
    }
    changed: list[str] = []
    for update in updates:
        if not isinstance(update, dict):
            continue
        step_id = update.get("step_id")
        if step_id not in declared:
            continue
        values = {
            field: update[field] for field in EDITABLE_FIELDS if field in update
        }
        if not values:
            continue
        # A cleared field (``null``) returns the step to the manifest's
        # default, so only the values actually being SET are checked.
        validate_override(step_id, {k: v for k, v in values.items() if v is not None})
        step = declared[step_id]
        values = {
            field: None if _is_default(value, getattr(step, field, None)) else value
            for field, value in values.items()
        }
        row = {
            "tenant_id": tenant_id,
            "agent_id": agent_id,
            "step_id": step_id,
            "updated_by": user_id,
            **{field: values.get(field) for field in EDITABLE_FIELDS},
        }
        statement = insert(AgentStepConfig).values(**row)
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id", "agent_id", "step_id"],
                set_={
                    **{field: statement.excluded[field] for field in values},
                    "updated_by": statement.excluded.updated_by,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )
        changed.append(step_id)
    if changed:
        # A row that now carries no override at all is deleted rather
        # than kept as a row of nulls: ``overrides_for`` already reads it
        # as nothing, and a table that accumulates one inert row per step
        # per tenant per Save is a table nobody can read.
        await db.execute(
            delete(AgentStepConfig).where(
                AgentStepConfig.tenant_id == tenant_id,
                AgentStepConfig.agent_id == agent_id,
                AgentStepConfig.step_id.in_(changed),
                *[getattr(AgentStepConfig, field).is_(None) for field in EDITABLE_FIELDS],
            )
        )
    await db.flush()
    return changed


async def clear(db: AsyncSession, tenant_id: UUID, agent_id: str) -> None:
    """Drop this tenant's overrides for one agent — back to the
    manifest's defaults."""
    await db.execute(
        delete(AgentStepConfig).where(
            AgentStepConfig.tenant_id == tenant_id,
            AgentStepConfig.agent_id == agent_id,
        )
    )
