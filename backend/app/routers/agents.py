"""HTTP surface for the pluggable agent framework.

- ``GET  /agents``                         list registered agents (authenticated)
- ``GET  /agents/{agent_id}/input-schema`` JSON Schema for the agent's wizard
- ``GET  /agents/{agent_id}/scenarios``    demo scenarios for intake prefill
- ``GET  /agents/{agent_id}/config``       admin-only: this tenant's steps and settings
- ``PUT  /agents/{agent_id}/config/steps`` admin-only: update per-step LLM params
- ``PUT  /agents/{agent_id}/config/settings`` admin-only: update this tenant's settings
- ``GET  /agents/{agent_id}/secrets``      admin-only: the declared tool secrets' state, never a value
- ``PUT  /agents/{agent_id}/secrets/{scope}/{name}``, ``DELETE`` the same:
  set or clear this tenant's value (``tenant``) or every tenant's default
  (``agent``, platform admins only) — K8a, D32

An agent's settings are declared in its manifest (``settings[]``) and
valued per tenant in ``agent_settings`` since K5a (L32). The protocol path
that preceded them — ``config_meta()``, ``get_settings()`` and
``update_settings()``, one value for every tenant — is served in the same
shapes for one release, marked ``meta.deprecated``, and removed at v1.2.

Audit writes use ``action_type="config_change"`` (the existing enum value the
legacy ``/admin/llm-config`` endpoint uses) so no DB CHECK-constraint
migration is required. The ``surface="agent_config"`` detail field
distinguishes agent-config edits from other config changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import structlog
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.manifest import AgentSettingSpec
from app.agents.protocol import AgentConfigMeta
from app.agents.registry import get_agent, get_agent_dir, get_manifest, list_agents
from app.database import get_db
from app.middleware import get_current_user, is_platform_admin, require_admin
from app.models import User
from app.schemas.agent_secrets import AgentSecretState, AgentSecretsList, SecretRow
from app.schemas.agent_settings import AgentSettingUpdate
from app.services import agent_settings_service as agent_settings
from app.services import agent_step_config_service as step_configs
from app.services import secrets_service
from app.services import tool_secrets_service as tool_secrets
from app.services.audit_service import log_audit
from app.services.intake import validate_user_inputs

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


def _agent_or_404(agent_id: str):
    agent = get_agent(agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown agent: {agent_id}")
    return agent


def _declares_llm_steps(agent_id: str) -> bool:
    manifest = get_manifest(agent_id)
    return bool(manifest is not None and manifest.llm and manifest.llm.steps)


def _config_meta(agent, agent_id: str) -> AgentConfigMeta:
    """The metadata the config page renders against.

    ``config_meta()`` is the AGENT's surface — the settings fields it
    invents for itself — and a container has no way to implement a
    Python method, so it returns None for every one of them. The LLM
    steps are not the agent's surface: they are the manifest's
    declaration and this tenant's rows, both of which the chassis holds.
    Gating the step editor on that method meant the reference container
    declared a step whose model was, by construction, editable nowhere
    (Codex P1) while the platform's own ``step_not_configured`` pointed
    at the page that hid it.

    So a manifest-only agent gets the defaults: the provider list and the
    editable fields the chassis knows. Its settings are not read here at
    all since K5a: they are the manifest's ``settings[]`` too.
    """
    return agent.config_meta() or AgentConfigMeta(settings=[])


def _declared_settings(agent_id: str) -> list[AgentSettingSpec]:
    manifest = get_manifest(agent_id)
    return list(manifest.settings) if manifest is not None else []


def _declared_secrets(agent_id: str) -> list[str]:
    """The tool-secret names the manifest declares (K8a): a Secrets tab's
    worth of configuration, so the agent page links to it."""
    manifest = get_manifest(agent_id)
    return list(manifest.secrets) if manifest is not None else []


def _deprecated_settings(agent, agent_id: str) -> list[AgentSettingSpec]:
    """The settings an agent serves through the deprecated protocol path
    (K5-06, L32): an agent whose manifest declares no ``settings[]`` and
    whose ``config_meta()`` returns some. A declaration turns the path off,
    so an agent moving onto ``settings[]`` is never served from both.
    Removed at v1.2 with the three protocol methods."""
    if _declared_settings(agent_id):
        return []
    return agent_settings.legacy_specs(agent.config_meta())


def _warn_deprecated(agent_id: str, method: str) -> None:
    """Every request the deprecated path serves says so, naming the agent
    that has still to declare ``settings[]`` before v1.2 removes it."""
    logger.warning(
        "agent_settings_protocol_deprecated",
        agent_id=agent_id,
        method=method,
        removed_in="v1.2",
        hint="declare the agent's settings in its manifest's settings[]: "
        "config_meta(), get_settings() and update_settings() are removed "
        "at v1.2, and their values are one value for every tenant",
    )


def _has_config_surface(agent, agent_id: str) -> bool:
    """Whether the agent page has a Steps or a Settings tab for this agent
    (K5a): LLM steps its manifest declares, ``settings[]`` it declares, or
    — until v1.2 — a ``config_meta()`` that returns settings. One
    predicate, used by the route that serves the page and by the listing
    that links to it."""
    return bool(
        _declares_llm_steps(agent_id)
        or _declared_settings(agent_id)
        or _declared_secrets(agent_id)
        or _deprecated_settings(agent, agent_id)
    )


def _require_config_surface(agent, agent_id: str) -> None:
    """404 only when there is nothing to configure at all — neither a
    declared LLM step nor a setting."""
    if not _has_config_surface(agent, agent_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Agent {agent_id} does not expose a config surface",
        )


def _scenario_dir(agent_id: str) -> Path | None:
    """Resolved scenarios directory for a discovered agent, or None.

    The manifest validator already forbids absolute/``..`` paths; the
    ``is_relative_to`` check is belt-and-braces so a future validator
    regression can't turn this into a filesystem browser.
    """
    manifest = get_manifest(agent_id)
    agent_dir = get_agent_dir(agent_id)
    if manifest is None or agent_dir is None:
        return None
    scen_dir = (agent_dir / manifest.scenarios).resolve()
    if not scen_dir.is_relative_to(agent_dir.resolve()):
        return None
    if not scen_dir.is_dir():
        return None
    return scen_dir


def _load_scenarios(agent_id: str) -> list[dict]:
    """Read + validate the agent's scenario JSON files.

    A scenario is ``{"name": str, "description"?: str, "user_inputs": dict}``
    where ``user_inputs`` is a valid ``POST /runs`` body — the same object
    prefills the intake form and can be submitted verbatim (the CI smoke
    run does exactly that, blueprint B14). That promise is enforced here:
    since B8 each agent owns its body shape, so ``user_inputs`` is
    validated against the agent's own ``input_schema()``. A malformed file
    is logged and skipped, never served half-broken.
    """
    scen_dir = _scenario_dir(agent_id)
    if scen_dir is None:
        return []
    agent = get_agent(agent_id)
    try:
        schema = agent.input_schema() if agent is not None else None
    except Exception:
        logger.exception("input_schema_unavailable", agent_id=agent_id)
        schema = None
    if schema is None:
        # No schema, no submittability check possible — and no submission
        # either (POST /runs 400s for the same reason), so don't offer
        # scenarios that can only dead-end.
        return []
    out: list[dict] = []
    for path in sorted(scen_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "agent_scenario_invalid",
                agent_id=agent_id,
                file=path.name,
                error=str(exc),
            )
            continue
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("name"), str)
            or not data["name"]
            or not isinstance(data.get("user_inputs"), dict)
        ):
            logger.warning(
                "agent_scenario_invalid",
                agent_id=agent_id,
                file=path.name,
                error="scenario needs a non-empty 'name' and a 'user_inputs' object",
            )
            continue
        schema_errors = validate_user_inputs(schema, data["user_inputs"])
        if schema_errors:
            logger.warning(
                "agent_scenario_invalid",
                agent_id=agent_id,
                file=path.name,
                error="user_inputs is not a submittable POST /runs body: "
                + "; ".join(schema_errors),
            )
            continue
        out.append(
            {
                "id": path.stem,
                "name": data["name"],
                "description": data.get("description", ""),
                "user_inputs": data["user_inputs"],
            }
        )
    return out


@router.get("")
async def list_agents_endpoint(
    _: User = Depends(get_current_user),
) -> list[dict]:
    """Summary of every discovered agent, manifest fields included.

    ``has_config`` tells the admin UI whether to render a config page or
    show "managed by the agent". It is the SAME predicate the config
    route uses, and has to be: fixing the route to serve a manifest-only
    agent's steps while this still asked ``config_meta()`` left a
    container agent's card saying "No admin config" with no link to the
    page that had just started working (Codex P1). A gate and the link
    to what it gates are one decision. ``phases``/``ui``/``output``/``capabilities``
    come from the agent's manifest (blueprint B7) so the frontend renders
    from data instead of special-casing agent ids; ``has_scenarios`` tells
    the intake page whether to offer the "Load scenario" control.
    """
    rows: list[dict] = []
    for a in list_agents():
        manifest = get_manifest(a.agent_id)
        rows.append(
            {
                "agent_id": a.agent_id,
                "display_name": a.display_name,
                "description": a.description,
                "has_config": _has_config_surface(a, a.agent_id),
                # Blueprint S7: each phase carries its declared progress
                # steps ``{id, label}`` so the run page can label rows
                # from data, and the manifest's ``framework`` is the badge
                # on the card (empty means "show the runtime").
                "phases": [
                    {
                        "name": p.name,
                        "approval": p.approval,
                        "steps": [{"id": st.id, "label": st.label} for st in p.steps],
                    }
                    for p in (manifest.phases if manifest else [])
                ],
                "framework": manifest.framework if manifest else "",
                "ui": {
                    "intake": (
                        manifest.ui.intake.model_dump() if manifest else {"steps": []}
                    )
                },
                "output": {"mode": manifest.output.mode if manifest else "html_report"},
                "capabilities": list(manifest.capabilities) if manifest else [],
                # Blueprint S4: the runtime and whether a container agent
                # opted out of "no path off-box but through the chassis" —
                # the admin page shows it beside the grants.
                "runtime": manifest.runtime if manifest else "python-package",
                "network": {"egress": bool(manifest.network.egress) if manifest else False},
                # Blueprint S4a: the agent's declared LLM steps and whether
                # the gateway redacts what it sends on their behalf — shown
                # beside the grants, because an agent that opted out of
                # outbound redaction is a thing an operator must be able to
                # see without reading the manifest.
                "llm": {
                    "steps": [
                        {"id": st.id, "label": st.label}
                        for st in (manifest.llm.steps if manifest else [])
                    ],
                    "redact_outbound": (
                        bool(manifest.llm.redact_outbound) if manifest else True
                    ),
                },
                # Feedback targets the results view renders (blueprint B9).
                "feedback_sections": [
                    {"id": s.id, "label": s.label}
                    for s in (manifest.feedback_sections if manifest else [])
                ],
                "has_scenarios": bool(_load_scenarios(a.agent_id)),
            }
        )
    return rows


@router.get("/{agent_id}/scenarios")
async def list_scenarios_endpoint(
    agent_id: str,
    _: User = Depends(get_current_user),
) -> list[dict]:
    """Demo scenarios for the intake "Load scenario" control.

    Each entry's ``user_inputs`` prefills the intake form and doubles as a
    submittable ``POST /runs`` body. An agent without a scenarios
    directory simply returns ``[]``.
    """
    _agent_or_404(agent_id)
    return _load_scenarios(agent_id)


@router.get("/{agent_id}/input-schema")
async def get_input_schema(
    agent_id: str,
    _: User = Depends(get_current_user),
) -> dict:
    agent = _agent_or_404(agent_id)
    return agent.input_schema()


@router.get("/{agent_id}/config")
async def get_config(
    agent_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """This tenant's effective configuration for one agent.

    The steps come from the agent's manifest (``llm.steps``) with this
    TENANT's overrides applied (blueprint S4a). They used to come from
    the agent instance, whose one implementation wrote one shared file —
    so a second tenant's edit overwrote the first and every tenant read
    the same value.

    The settings follow them since K5a (L32): ``meta.settings`` is the
    manifest's ``settings[]`` and ``settings`` this tenant's effective
    values, each marked ``overridden`` where it is the tenant's choice
    rather than the default. An agent still on the deprecated protocol
    path is served in the same shapes, its values ``get_settings()``'s —
    one for every tenant — and ``meta.deprecated`` says so, which is what
    the Settings tab shows in place of the tenant's scope.
    """
    agent = _agent_or_404(agent_id)
    _require_config_surface(agent, agent_id)
    meta = _config_meta(agent, agent_id)
    manifest = get_manifest(agent_id)
    overrides = await step_configs.overrides_for(db, user.tenant_id, agent_id)
    specs = _deprecated_settings(agent, agent_id)
    deprecated = bool(specs)
    if deprecated:
        _warn_deprecated(agent_id, "get_settings")
        settings = agent_settings.legacy_effective(specs, agent.get_settings())
    else:
        specs = _declared_settings(agent_id)
        # An agent that declares none has nothing to read: no query.
        values = (
            await agent_settings.values_for(db, user.tenant_id, agent_id) if specs else {}
        )
        settings = agent_settings.effective(manifest, values)
    return {
        "meta": {
            "supported_providers": list(meta.supported_providers),
            "step_editable_fields": list(meta.step_editable_fields),
            "settings": [spec.model_dump() for spec in specs],
            "deprecated": deprecated,
            # K8a: the tool secrets the manifest declares, by name — the
            # Secrets tab reads their state from ``GET …/secrets``.
            "secrets": list(manifest.secrets) if manifest is not None else [],
        },
        "steps": step_configs.effective_steps(manifest, overrides),
        "settings": settings,
    }


@router.put("/{agent_id}/config/steps", status_code=status.HTTP_204_NO_CONTENT)
async def update_steps(
    agent_id: str,
    request: Request,
    payload: list[dict] = Body(...),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    agent = _agent_or_404(agent_id)
    _require_config_surface(agent, agent_id)
    try:
        changed = await step_configs.apply_updates(
            db,
            user.tenant_id,
            agent_id,
            get_manifest(agent_id),
            payload,
            user.id,
        )
    except step_configs.InvalidStepOverride as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    ip = request.client.host if request.client else None
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {
            "surface": "agent_config",
            "agent_id": agent_id,
            "section": "steps",
            "changed_step_ids": changed,
        },
        ip,
    )
    return None


@router.put("/{agent_id}/config/settings", status_code=status.HTTP_204_NO_CONTENT)
async def update_settings_endpoint(
    agent_id: str,
    request: Request,
    payload: list[AgentSettingUpdate] = Body(...),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Save this tenant's settings: ``[{key, value}]``, ``null`` for the
    default (K5a).

    Every value is held to the type its setting declares before anything
    is written, and one refusal — a key the agent does not declare, a
    value its type refuses — answers ``400 setting '<key>': …`` and writes
    nothing. A value equal to the default is stored as no row at all
    (D17). The deprecated protocol path takes the same body and makes one
    ``update_settings({key: value})`` call, ``null`` sending the declared
    default. The audit row names the keys whose value changed.
    """
    agent = _agent_or_404(agent_id)
    legacy = _deprecated_settings(agent, agent_id)
    if not legacy and not _declared_settings(agent_id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"Agent {agent_id} declares no settings",
        )
    updates = [{"key": item.key, "value": item.value} for item in payload]
    try:
        if legacy:
            _warn_deprecated(agent_id, "update_settings")
            values = agent_settings.legacy_updates(legacy, updates)
            before = agent.get_settings()
            agent.update_settings(values)
            changed = agent_settings.legacy_changed(before, values)
        else:
            changed = await agent_settings.apply_updates(
                db,
                user.tenant_id,
                agent_id,
                get_manifest(agent_id),
                updates,
                user.id,
            )
    except agent_settings.InvalidSettingValue as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    ip = request.client.host if request.client else None
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {
            "surface": "agent_config",
            "agent_id": agent_id,
            "section": "settings",
            "changed_keys": changed,
        },
        ip,
    )
    return None


# -- tool secrets (K8a; D20, D32, L31) -----------------------------------------


def _secret_refusal(status_code: int, code: str, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail, "code": code})


def _row(state: tool_secrets.RowState, *, show_by: bool = True) -> SecretRow:
    return SecretRow(
        set=state.set,
        fingerprint=state.fingerprint,
        updated_at=state.updated_at,
        updated_by=state.updated_by if show_by else None,
        last_used_at=state.last_used_at,
    )


def _secret_state(state: tool_secrets.SecretState, *, platform: bool) -> AgentSecretState:
    return AgentSecretState(
        name=state.name,
        effective=state.effective,
        tenant=_row(state.tenant),
        # Every tenant's default is a platform admin's row: who set it is
        # the operator's to see (D32).
        agent=_row(state.agent, show_by=platform),
        environment=None if state.environment is None else SecretRow(set=state.environment),
    )


async def _secret_write_refusal(
    agent_id: str, scope: str, name: str, user: User, db: AsyncSession
):
    """The refusals a write shares with a clear, in the order a caller can
    act on them: the agent, the scope, the name, then who may write it."""
    _agent_or_404(agent_id)
    manifest = get_manifest(agent_id)
    if scope not in tool_secrets.SCOPES:
        return None, _secret_refusal(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "secret_scope_invalid",
            f"a tool secret's scope is 'tenant' or 'agent', not {scope!r}",
        )
    if manifest is None or name not in manifest.secrets:
        return None, _secret_refusal(
            status.HTTP_404_NOT_FOUND,
            tool_secrets.SecretNotDeclared.code,
            str(tool_secrets.SecretNotDeclared(name)),
        )
    if scope == "agent" and not await is_platform_admin(user, db):
        # Every tenant's default is application-global: one tenant's admin
        # may not choose what another tenant's runs read (D32).
        return None, _secret_refusal(
            status.HTTP_403_FORBIDDEN,
            "platform_admin_only",
            "Platform operator only — a tool secret's default is every tenant's",
        )
    return manifest, None


async def _audit_secret(
    db: AsyncSession, request: Request, user: User, agent_id: str, name: str, scope: str, action: str
) -> None:
    await log_audit(
        db,
        user.tenant_id,
        user.id,
        user.email,
        "config_change",
        {
            "surface": "agent_secrets",
            "agent_id": agent_id,
            "name": name,
            "scope": scope,
            "action": action,
        },
        request.client.host if request.client else None,
    )


@router.get(
    "/{agent_id}/secrets",
    response_model=AgentSecretsList,
    summary="The declared tool secrets' state in this tenant, never a value",
)
async def list_agent_secrets(
    agent_id: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> AgentSecretsList:
    """Each name the manifest declares in ``secrets[]``: this tenant's row,
    every tenant's default and, for an in-process agent, the environment's
    fallback — each set or not, its fingerprint, who and when, and when a
    run last read it — and ``effective``, where a run of this tenant reads
    it now. Never a value (L31)."""
    _agent_or_404(agent_id)
    manifest = get_manifest(agent_id)
    platform = await is_platform_admin(user, db)
    states = await tool_secrets.states(db, manifest, user.tenant_id) if manifest else []
    return AgentSecretsList(
        agent_id=agent_id,
        runtime=manifest.runtime if manifest else "python-package",
        secrets=[_secret_state(state, platform=platform) for state in states],
    )


@router.put(
    "/{agent_id}/secrets/{scope}/{name}",
    response_model=AgentSecretState,
    summary="Set a tool secret's value: this tenant's, or every tenant's default",
    responses={
        400: {"description": "secret_value_invalid: the body is not {value} with 8 to 4096 characters"},
        403: {"description": "platform_admin_only: the default (scope agent) is a platform admin's"},
        404: {"description": "unknown agent, or secret_not_declared"},
        422: {"description": "secret_scope_invalid: the scope is not tenant or agent"},
        503: {"description": "secrets_store_unconfigured or secrets_store_key_shared"},
    },
)
async def set_agent_secret(
    agent_id: str,
    scope: str,
    name: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """``{value}`` → ``200`` and the name's state. The body is read by hand,
    so no refusal can echo it, and the value is stored stripped. Audited
    ``{surface: agent_secrets, agent_id, name, scope, action: set |
    replace}`` without the value; the row and its audit commit together,
    and only then is every process told."""
    manifest, refusal = await _secret_write_refusal(agent_id, scope, name, user, db)
    if refusal is not None:
        return refusal
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict) or set(body) != {"value"}:
        return _secret_refusal(
            status.HTTP_400_BAD_REQUEST,
            tool_secrets.SecretValueInvalid.code,
            "the body is a JSON object with one field, value",
        )
    try:
        write = await tool_secrets.set_value(
            db, manifest, scope, user.tenant_id, name, body["value"], user_id=user.id
        )
    except tool_secrets.SecretValueInvalid as exc:
        return _secret_refusal(status.HTTP_400_BAD_REQUEST, exc.code, str(exc))
    await _audit_secret(db, request, user, agent_id, name, scope, write.action)
    # The answer is read inside the transaction, where the row already
    # stands, so a failed read is never a 500 for a write already durable
    # (Codex on #172); then the commit, and only then is every process
    # told — told first, one reading in between would keep the old value
    # for the cache's 30 seconds (§11, the review after K7).
    platform = await is_platform_admin(user, db)
    (state,) = [s for s in await tool_secrets.states(db, manifest, user.tenant_id) if s.name == name]
    answer = _secret_state(state, platform=platform)
    await db.commit()
    await secrets_service.notify_change(tool_secrets.owner(scope, user.tenant_id, agent_id), name)
    return answer


@router.delete(
    "/{agent_id}/secrets/{scope}/{name}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Clear a tool secret's value; the next source in the order serves",
    responses={
        403: {"description": "platform_admin_only: the default (scope agent) is a platform admin's"},
        404: {"description": "unknown agent, or secret_not_declared"},
        422: {"description": "secret_scope_invalid: the scope is not tenant or agent"},
    },
)
async def clear_agent_secret(
    agent_id: str,
    scope: str,
    name: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """``204``, whether or not there was a row — idempotent, and it needs no
    store key, so a row sealed under a lost key can still be cleared.
    Audited ``{…, action: clear}``; committed, then every process told."""
    manifest, refusal = await _secret_write_refusal(agent_id, scope, name, user, db)
    if refusal is not None:
        return refusal
    await tool_secrets.clear_value(db, manifest, scope, user.tenant_id, name)
    await _audit_secret(db, request, user, agent_id, name, scope, "clear")
    await db.commit()
    await secrets_service.notify_change(tool_secrets.owner(scope, user.tenant_id, agent_id), name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
