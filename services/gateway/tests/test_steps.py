"""Step resolution: the admin's choice wins, and an undeclared id is refused."""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from gateway import errors, steps
from gateway.auth import Principal
from gateway.snapshots import Snapshot

from conftest import manifest_of


def _principal(agent_id: str, tenant_id: str | None = None, **manifest_overrides):
    """A principal as `authenticate` would build one.

    `token_grants` mirrors the manifest because that is what a run token
    minted for this agent, now, would carry. It used to be left empty
    and nothing noticed, because `grants()` read only the snapshot — the
    defect Codex found in round 17. A run-token principal with no grants
    is a token that was issued granting nothing, and it is refused; a
    double that models one is modelling the wrong thing.
    """
    manifest = manifest_of(agent_id, **manifest_overrides)
    return Principal(
        agent_id=agent_id,
        snapshot=Snapshot(agent_id, manifest),
        tenant_id=tenant_id,
        run_id=str(uuid.uuid4()) if tenant_id else None,
        token_grants=list(manifest.get("capabilities") or []),
    )


async def _tenant(session) -> str:
    slug = f"gw-{uuid.uuid4().hex[:8]}"
    return str(
        (
            await session.execute(
                text("INSERT INTO tenants (name, slug) VALUES (:n, :s) RETURNING id"),
                {"n": slug, "s": slug},
            )
        ).scalar_one()
    )


async def _override(session, tenant_id, agent_id, step_id, **values):
    columns = ", ".join(values)
    params = ", ".join(f":{k}" for k in values)
    await session.execute(
        text(
            f"INSERT INTO agent_step_configs "
            f"(tenant_id, agent_id, step_id, {columns}) "
            f"VALUES (CAST(:t AS UUID), :a, :s, {params})"
        ),
        {"t": tenant_id, "a": agent_id, "s": step_id, **values},
    )


# ----------------------------- naming the step ------------------------------


def test_the_header_wins_over_the_model_string():
    assert steps.requested_step_id("think", "librerun/draft") == "think"
    assert steps.requested_step_id(None, "librerun/draft") == "draft"
    assert steps.requested_step_id("  ", "librerun/draft") == "draft"
    # A plain model name names no step: the model field is a selector,
    # not a choice the agent gets to make.
    assert steps.requested_step_id(None, "gpt-4o") is None
    assert steps.requested_step_id(None, None) is None


@pytest.mark.asyncio
async def test_the_declared_defaults_resolve_with_no_override(session, agent_id):
    resolved = await steps.resolve(
        session, _principal(agent_id), step_header="think", model=None
    )

    assert resolved.step_id == "think"
    assert (resolved.provider, resolved.model) == ("openai", "gpt-4o")
    assert (resolved.max_tokens, resolved.timeout_seconds) == (100, 30)
    assert resolved.target == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_a_tenants_override_replaces_the_values_it_sets(session, agent_id):
    """D13: change the model in the admin UI and the next call uses it,
    with nothing restarted. Fields the override leaves null keep the
    manifest's defaults — an admin who changed only the temperature has
    not silently unset the token limit."""
    tenant = await _tenant(session)
    await _override(
        session, tenant, agent_id, "think", model="gpt-4o-mini", temperature=0.7
    )

    resolved = await steps.resolve(
        session, _principal(agent_id, tenant), step_header="think", model=None
    )

    assert resolved.model == "gpt-4o-mini"
    assert resolved.temperature == 0.7
    assert resolved.provider == "openai"  # untouched by the override
    assert resolved.max_tokens == 100


@pytest.mark.asyncio
async def test_one_tenants_override_is_invisible_to_another(session, agent_id):
    a, b = await _tenant(session), await _tenant(session)
    await _override(session, a, agent_id, "think", model="tenant-a-model")
    await _override(session, b, agent_id, "think", model="tenant-b-model")

    first = await steps.resolve(
        session, _principal(agent_id, a), step_header="think", model=None
    )
    second = await steps.resolve(
        session, _principal(agent_id, b), step_header="think", model=None
    )

    assert first.model == "tenant-a-model"
    assert second.model == "tenant-b-model"


@pytest.mark.asyncio
async def test_an_undeclared_step_is_refused(session, agent_id):
    with pytest.raises(errors.GatewayError) as exc:
        await steps.resolve(
            session, _principal(agent_id), step_header="invented", model=None
        )
    assert exc.value.code == "unknown_step"
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_an_override_for_a_step_the_manifest_dropped_is_inert(
    session, agent_id
):
    """A tenant override never DECLARES a step; it only replaces values
    for one the manifest still declares. So a row left behind by a
    removed or renamed step cannot make the retired step callable
    again."""
    tenant = await _tenant(session)
    await _override(
        session, tenant, agent_id, "retired", provider="openai", model="gpt-4o"
    )

    with pytest.raises(errors.GatewayError) as exc:
        await steps.resolve(
            session,
            _principal(agent_id, tenant),
            step_header="retired",
            model=None,
        )
    assert exc.value.code == "unknown_step"


@pytest.mark.asyncio
async def test_a_call_naming_no_step_is_refused_without_a_default(session, agent_id):
    with pytest.raises(errors.GatewayError) as exc:
        await steps.resolve(session, _principal(agent_id), step_header=None, model="gpt-4o")
    assert exc.value.code == "step_required"


@pytest.mark.asyncio
async def test_a_declared_default_step_catches_a_call_that_names_none(
    session, agent_id
):
    principal = _principal(
        agent_id,
        llm={
            "steps": [
                {"id": "default", "provider": "anthropic", "model": "claude-sonnet-4-6"}
            ]
        },
    )

    resolved = await steps.resolve(session, principal, step_header=None, model=None)

    assert resolved.step_id == "default"
    assert resolved.target == "anthropic/claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_an_agent_with_no_llm_grant_cannot_resolve_a_step(session, agent_id):
    principal = _principal(agent_id, capabilities=["kb"], llm={"steps": []})

    with pytest.raises(errors.GatewayError) as exc:
        await steps.resolve(session, principal, step_header="think", model=None)
    assert exc.value.code == "llm_not_granted"
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_a_step_with_nothing_configured_is_refused_not_guessed(
    session, agent_id
):
    principal = _principal(agent_id, llm={"steps": [{"id": "think"}]})

    with pytest.raises(errors.GatewayError) as exc:
        await steps.resolve(session, principal, step_header="think", model=None)
    assert exc.value.code == "step_not_configured"


@pytest.mark.asyncio
async def test_stub_mode_makes_stub_the_provider_for_every_step(
    session, agent_id, monkeypatch
):
    monkeypatch.setattr(steps.settings, "LIBRERUN_STUB_LLM", True)

    resolved = await steps.resolve(
        session, _principal(agent_id), step_header="think", model=None
    )

    assert resolved.provider == "stub"
    assert resolved.model == "gpt-4o"  # still says which model it stands in for


# ------------------------ the platform's own step ---------------------------


@pytest.mark.asyncio
async def test_kb_embed_needs_the_kb_grant_not_the_llm_one(session, agent_id):
    llm_only = _principal(agent_id)

    with pytest.raises(errors.GatewayError) as exc:
        await steps.resolve(
            session, llm_only, step_header="kb_embed", model=None, embedding=True
        )
    assert exc.value.code == "kb_not_granted"

    kb_only = _principal(agent_id, capabilities=["kb"], llm={"steps": []})
    resolved = await steps.resolve(
        session, kb_only, step_header="kb_embed", model=None, embedding=True
    )
    assert resolved.platform is True
    assert resolved.target == "openai/text-embedding-3-small"


@pytest.mark.asyncio
async def test_kb_embed_is_not_a_chat_step(session, agent_id):
    kb_only = _principal(agent_id, capabilities=["kb"], llm={"steps": []})

    with pytest.raises(errors.GatewayError) as exc:
        await steps.resolve(
            session, kb_only, step_header="kb_embed", model=None, embedding=False
        )
    assert exc.value.code == "unknown_step"


@pytest.mark.asyncio
async def test_the_platform_setting_routes_kb_embed_with_no_restart(session, agent_id):
    kb_only = _principal(agent_id, capabilities=["kb"], llm={"steps": []})
    assert (
        await steps.resolve(
            session, kb_only, step_header="kb_embed", model=None, embedding=True
        )
    ).target == "openai/text-embedding-3-small"

    await session.execute(
        text(
            "INSERT INTO app_settings (key, value) VALUES (:k, CAST(:v AS JSONB)) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        ),
        {"k": steps.KB_EMBED_SETTING, "v": '"openai/text-embedding-3-large"'},
    )

    again = await steps.resolve(
        session, kb_only, step_header="kb_embed", model=None, embedding=True
    )
    assert again.target == "openai/text-embedding-3-large"


@pytest.mark.asyncio
async def test_an_agent_cannot_route_the_platforms_embedding_step(session, agent_id):
    """kb_embed is never taken from an agent's llm.steps. The chassis
    validator refuses to declare it at all, so the snapshot cannot carry
    one — and even a hand-built snapshot that did is ignored here."""
    forged = _principal(agent_id, capabilities=["kb", "llm"])
    forged.snapshot.manifest["llm"]["steps"].append(
        {"id": "kb_embed", "provider": "openai", "model": "an-expensive-model"}
    )

    resolved = await steps.resolve(
        session, forged, step_header="kb_embed", model=None, embedding=True
    )

    assert resolved.model == "text-embedding-3-small"


def test_kb_embed_is_bounded_to_what_search_already_allows():
    steps.check_kb_embed_bounds(["one", "two"])

    with pytest.raises(errors.GatewayError) as exc:
        steps.check_kb_embed_bounds(
            ["q"] * (steps.settings.KB_EMBED_MAX_QUERIES + 1)
        )
    assert exc.value.code == "kb_embed_bounds"

    with pytest.raises(errors.GatewayError) as exc:
        steps.check_kb_embed_bounds(["q" * (steps.settings.KB_EMBED_MAX_QUERY_CHARS + 1)])
    assert exc.value.code == "kb_embed_bounds"

    # Exactly at the bound is inside it.
    steps.check_kb_embed_bounds(["q"] * steps.settings.KB_EMBED_MAX_QUERIES)
    steps.check_kb_embed_bounds(["q" * steps.settings.KB_EMBED_MAX_QUERY_CHARS])
