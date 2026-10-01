"""Who may call the gateway, with which credential, for what (D10)."""
from __future__ import annotations

import pytest

from gateway import keys
from gateway.auth import RUN_TOKEN_HEADER


def _code(response) -> str:
    return response.json()["error"]["code"]


@pytest.mark.asyncio
async def test_healthz_is_unauthenticated_and_says_three_things(client):
    """The backend's /api/v1/meta reads this and presents no credential,
    so anything beyond these is an unauthenticated disclosure about the
    deployment. A CLOSED set, deliberately: the guard is that a field
    cannot arrive here without someone arguing for it.

    ``pii_detector`` was argued for at blueprint S4c and is two fields
    exactly — outbound redaction runs in THIS process, so whether its
    named-entity stage works is a question about the gateway that no
    other endpoint can answer, and an agent container meeting
    ``pii_detector_unavailable`` has to be able to tell that from a
    provider outage. The backend's /health carries the failing
    exception's class as well; this one does not, because every agent
    container on the network can reach it.
    """
    response = await client.get("/healthz")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "stub", "pii_detector"}
    assert response.json()["status"] == "ok"
    assert isinstance(response.json()["stub"], bool)
    assert set(response.json()["pii_detector"]) == {"state", "coverage"}


@pytest.mark.asyncio
async def test_no_credential_at_all_is_refused(client):
    response = await client.get("/v1/models")

    assert response.status_code == 401
    assert _code(response) == "credential_required"


@pytest.mark.asyncio
async def test_an_agent_key_alone_lists_that_agents_steps(client, installed):
    response = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {installed.key}"}
    )

    assert response.status_code == 200
    assert [m["id"] for m in response.json()["data"]] == ["librerun/think"]
    assert {m["owned_by"] for m in response.json()["data"]} == {"librerun"}


@pytest.mark.asyncio
async def test_a_provider_key_is_never_accepted(client):
    response = await client.get(
        "/v1/models", headers={"Authorization": "Bearer sk-not-a-librerun-key"}
    )

    assert response.status_code == 401
    assert _code(response) == "agent_key_invalid"


@pytest.mark.asyncio
async def test_a_run_token_alone_is_enough_to_list(client, run_token):
    """The in-process capability presents a run token and no agent key —
    the backend holds none."""
    token = await run_token()

    response = await client.get("/v1/models", headers={RUN_TOKEN_HEADER: token})

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_an_ended_token_may_carry_telemetry_but_not_spend(client, run_token):
    token = await run_token(state="ended")

    response = await client.get("/v1/models", headers={RUN_TOKEN_HEADER: token})

    assert response.status_code == 401
    assert _code(response) == "run_token_ended"


@pytest.mark.asyncio
async def test_an_unknown_run_token_is_refused(client):
    response = await client.get(
        "/v1/models", headers={RUN_TOKEN_HEADER: "not-a-token-at-all"}
    )

    assert response.status_code == 401
    assert _code(response) == "run_token_invalid"


@pytest.mark.asyncio
async def test_a_key_and_a_token_naming_different_agents_are_refused(
    client, installed, run_token
):
    token = await run_token(agent_id="someone-else-v1")

    response = await client.get(
        "/v1/models",
        headers={
            "Authorization": f"Bearer {installed.key}",
            RUN_TOKEN_HEADER: token,
        },
    )

    assert response.status_code == 401
    assert _code(response) == "credential_mismatch"


@pytest.mark.asyncio
async def test_an_agent_with_no_llm_grant_is_refused(client, installed, manifest):
    """The manifest snapshot is the authority, whatever credential the
    caller holds."""
    await installed.reinstall(
        manifest(installed.id, capabilities=["kb"], llm={"steps": []})
    )

    response = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {installed.key}"}
    )

    assert response.status_code == 403
    assert _code(response) == "llm_not_granted"


@pytest.mark.asyncio
async def test_an_uninstalled_agent_is_no_agent(client, installed):
    """A snapshot stamped absent means the directory is gone or the
    manifest stopped validating; either way nothing it once declared
    still authorizes a call. Reinstalling clears it."""
    await installed.reinstall(absent=True)

    response = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {installed.key}"}
    )
    assert response.status_code == 403
    assert _code(response) == "agent_unknown"

    await installed.reinstall()
    again = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {installed.key}"}
    )
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_a_key_for_an_agent_with_no_snapshot_at_all_is_refused(client, session):
    """A key row can outlive the manifest that justified it — rows are
    never deleted — so the snapshot, not the key, decides."""
    orphan = "orphan-agent-v1"
    value = keys.mint_key()
    await keys.reconcile_env_keys(session, {keys.env_name_for(orphan): value})

    response = await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {value}"}
    )

    assert response.status_code == 403
    assert _code(response) == "agent_unknown"


@pytest.mark.asyncio
async def test_a_used_key_records_when_through_the_app(client, session, installed):
    from sqlalchemy import text

    await client.get(
        "/v1/models", headers={"Authorization": f"Bearer {installed.key}"}
    )

    used = (
        await session.execute(
            text("SELECT last_used_at FROM agent_keys WHERE agent_id = :a"),
            {"a": installed.id},
        )
    ).scalar_one()
    assert used is not None
