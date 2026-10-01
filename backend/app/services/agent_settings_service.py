"""A tenant's values for the settings an agent declares (K5a, L32, D17).

An agent declares its settings in its manifest — ``settings[]``, each a
key, a type and a default — and each tenant's admin edits the values this
tenant's runs read, on the agent page's Settings tab
(``PUT /agents/{id}/config/settings``). A run reads them through the
façade's ``config`` member or the MCP ``config_get`` tool. The values live
in ``agent_settings``, keyed by ``(tenant_id, agent_id, key)``: the demo
agent's overlay is keyed by the agent alone, so one tenant's edit there is
every tenant's value — the defect S4a fixed for steps (§1.1, T9).

A row records a DIVERGENCE (D17), the rule ``agent_step_config_service``
already follows for steps and for the same reason: the page posts every
value it shows, defaults included, and a value equal to the default is not
a choice anyone made. So it is not stored, and the agent's next release
can move a default and reach every tenant that never chose one.

A row the manifest no longer describes is inert: its key is no longer
declared (the setting was removed or renamed), or its value is one the
declaration no longer accepts (its type changed). The default is served
in its place, never a value of the wrong type.

The bottom of this module serves the deprecated path (K5-06): an agent
that declares no ``settings[]`` and still answers ``config_meta()`` with
settings has them served in the same shapes until v1.2, when the three
protocol methods go (L32). Only the router calls it.
"""
from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manifest import AgentSettingSpec
from app.models import AgentSetting


class InvalidSettingValue(ValueError):
    """An update naming a key the agent does not declare, or carrying a
    value the setting's type refuses. The router answers it 400."""

    def __init__(self, key: str, detail: str):
        super().__init__(f"setting {key!r}: {detail}")
        self.key = key
        self.detail = detail


def _same(a: Any, b: Any) -> bool:
    """Equality that does not take a boolean for a number (K5-07).

    ``True == 1`` in Python, so a plain ``==`` would call a stored ``1``
    the value ``true`` and a stored ``true`` the integer ``1``. Values of
    one setting are compared after ``coerce``, which has already given
    them the setting's own type, so the type check costs nothing there;
    where it matters is a row written under an older declaration.
    """
    return type(a) is type(b) and a == b


async def values_for(
    db: AsyncSession, tenant_id: UUID, agent_id: str
) -> dict[str, Any]:
    """This tenant's rows for one agent, ``{key: value}``, as stored:
    divergences only, and possibly rows the manifest no longer describes.
    ``effective`` is what reads them against the declaration."""
    rows = await db.execute(
        select(AgentSetting.key, AgentSetting.value).where(
            AgentSetting.tenant_id == tenant_id,
            AgentSetting.agent_id == agent_id,
        )
    )
    return {key: value for key, value in rows.all()}


def _default(spec: AgentSettingSpec) -> Any:
    """The declared default, as a copy its caller may change.

    A ``string_list`` default is a list on the manifest the registry holds
    for the life of the worker. Handed out as it is, a run that appended
    to what ``settings()`` gave it would change the default that every
    later run, of every tenant, reads, and that D17 compares each Save
    with.
    """
    return copy.deepcopy(spec.default)


def _held(spec: AgentSettingSpec, values: Mapping[str, Any]) -> tuple[Any, bool]:
    """The value a run reads for ``spec``, and whether it is this tenant's."""
    if spec.key not in values:
        return _default(spec), False
    try:
        value = spec.coerce(values[spec.key])
    except ValueError:
        # Written under a declaration this one has replaced: inert.
        return _default(spec), False
    if _same(value, spec.default):
        return _default(spec), False
    return value, True


def effective(manifest, values: Mapping[str, Any]) -> list[dict]:
    """The settings this tenant's runs read, in the manifest's order.

    ``overridden`` says which values are this tenant's choice rather than
    the agent's default, so the Settings tab can mark them and offer a
    Reset.
    """
    out = []
    for spec in manifest.settings if manifest is not None else []:
        value, overridden = _held(spec, values)
        out.append(
            {
                "key": spec.key,
                "label": spec.label,
                "type": spec.type,
                "value": value,
                "default": _default(spec),
                "overridden": overridden,
            }
        )
    return out


def effective_values(manifest, values: Mapping[str, Any]) -> dict[str, Any]:
    """``{key: value}`` of ``effective`` — what a run is handed."""
    return {entry["key"]: entry["value"] for entry in effective(manifest, values)}


def _checked(
    specs: Mapping[str, AgentSettingSpec], updates: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """Every update held to its declaration before anything is written:
    ``{key: the value a run should read, or None for the default}``.

    One refusal refuses the request, so a Save never lands half of what it
    sent. ``null`` is the clear: it asks for the default, whatever the
    type. A key named twice is refused rather than resolved by order.
    """
    checked: dict[str, Any] = {}
    for update in updates:
        key = update["key"]
        value = update["value"]
        spec = specs.get(key)
        if spec is None:
            raise InvalidSettingValue(key, "this agent declares no such setting")
        if key in checked:
            raise InvalidSettingValue(key, "is named more than once in one request")
        if value is None:
            checked[key] = None
            continue
        try:
            checked[key] = spec.coerce(value)
        except ValueError as exc:
            raise InvalidSettingValue(key, str(exc)) from None
    return checked


async def apply_updates(
    db: AsyncSession,
    tenant_id: UUID,
    agent_id: str,
    manifest,
    updates: Iterable[Mapping[str, Any]],
    user_id: UUID | None,
) -> list[str]:
    """Write a tenant's edits; returns the keys whose row changed.

    Each value is coerced to its setting's type BEFORE it is compared with
    the default (K5-07): a number input sends ``0`` where the manifest
    declares ``0.0``, and those are one value, while ``1`` is refused for
    a boolean rather than stored as ``true``. A value equal to the default
    — or ``null`` — deletes the row (D17); anything else is upserted. A key
    whose row would not change is left alone, so the audit's
    ``changed_keys`` names what an admin changed, not everything the page
    posted.
    """
    specs = {
        spec.key: spec for spec in (manifest.settings if manifest is not None else [])
    }
    checked = _checked(specs, updates)
    stored = await values_for(db, tenant_id, agent_id)
    changed: list[str] = []
    for key, value in checked.items():
        if value is None or _same(value, specs[key].default):
            if key not in stored:
                continue
            await db.execute(
                delete(AgentSetting).where(
                    AgentSetting.tenant_id == tenant_id,
                    AgentSetting.agent_id == agent_id,
                    AgentSetting.key == key,
                )
            )
        else:
            if key in stored and _same(stored[key], value):
                continue
            statement = insert(AgentSetting).values(
                tenant_id=tenant_id,
                agent_id=agent_id,
                key=key,
                value=value,
                updated_by=user_id,
            )
            await db.execute(
                statement.on_conflict_do_update(
                    index_elements=["tenant_id", "agent_id", "key"],
                    set_={
                        "value": statement.excluded.value,
                        "updated_by": statement.excluded.updated_by,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
        changed.append(key)
    await db.flush()
    return changed


# --- the deprecated path (K5-06), served until v1.2 ------------------------


def legacy_specs(meta) -> list[AgentSettingSpec]:
    """``config_meta().settings`` as ``AgentSettingSpec``s, so the deprecated
    path is served in the new shapes and held by the same ``coerce``.

    ``field_type`` is ``type`` and ``enum_options`` is ``options``. A field
    the new model would refuse — a key outside the charset, a default of
    the wrong type — is still served, built without validation: this path
    exists so that nothing an agent served before K5a stops working, and
    the chassis never checked these fields before.
    """
    specs = []
    for field in getattr(meta, "settings", None) or []:
        fields = {
            "key": field.key,
            "label": field.label,
            "type": field.field_type,
            "default": field.default,
            "options": field.enum_options,
            "description": field.description,
        }
        try:
            specs.append(AgentSettingSpec.model_validate(fields))
        except ValueError:
            specs.append(AgentSettingSpec.model_construct(**fields))
    return specs


def _differs(spec: AgentSettingSpec, value: Any) -> bool:
    """Whether a value from ``get_settings()`` is not the declared default,
    compared after ``coerce`` where the value allows it."""
    try:
        value = spec.coerce(value)
        default = spec.coerce(spec.default)
    except ValueError:
        return value != spec.default
    return not _same(value, default)


def legacy_effective(specs: list[AgentSettingSpec], current: Mapping[str, Any]) -> list[dict]:
    """``effective``'s shape over ``get_settings()``'s answer: one value for
    every tenant, which is exactly what the deprecation notice says."""
    return [
        {
            "key": spec.key,
            "label": spec.label,
            "type": spec.type,
            "value": current.get(spec.key, spec.default),
            "default": spec.default,
            "overridden": spec.key in current and _differs(spec, current[spec.key]),
        }
        for spec in specs
    ]


def legacy_updates(
    specs: list[AgentSettingSpec], updates: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """The one ``update_settings({key: value})`` a deprecated PUT makes:
    each value held by ``coerce``, ``null`` sending the declared default."""
    by_key = {spec.key: spec for spec in specs}
    return {
        key: by_key[key].default if value is None else value
        for key, value in _checked(by_key, updates).items()
    }


def legacy_changed(before: Mapping[str, Any], values: Mapping[str, Any]) -> list[str]:
    """The keys of a deprecated PUT whose value is not what
    ``get_settings()`` answered before it — the audit's ``changed_keys``."""
    return [
        key for key, value in values.items() if not (key in before and _same(before[key], value))
    ]
