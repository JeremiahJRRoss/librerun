"""The agent-key endpoints, typed (K blueprint K9-08, K9-02; D10).

``GET /admin/agent-keys`` answers ``AgentKeyRow``s — never a value — with
whether the page may rotate a key and whether its agent is registered;
issue and rotate answer the one response that carries a value; and an
agent id a manifest could not declare is a 422 before anything is asked of
the database.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.agents import manifest as manifest_module
from app.routers import admin as admin_router
from app.services.agent_key_service import IssuedKey
from tests.test_handler_elements_evaluated import _client

ROW_FIELDS = {
    "agent_id",
    "key_prefix",
    "source",
    "role",
    "issued_at",
    "issued_by",
    "previous_since",
    "previous_until",
    "last_used_at",
    "rotatable",
    "registered",
}


def _row(agent_id: str, source: str, role: str = "current", *, issued_by=None) -> dict:
    return {
        "agent_id": agent_id,
        "key_prefix": "a1b2c3d4",
        "source": source,
        "role": role,
        "issued_at": datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
        "issued_by": issued_by,
        "previous_since": None,
        "previous_until": None,
        "last_used_at": None,
    }


def test_the_list_is_typed_and_says_registered(monkeypatch):
    issuer = uuid.uuid4()
    rows = [
        _row("agent-here", "admin", issued_by=issuer),
        _row("agent-here", "admin", role="previous", issued_by=issuer),
        _row("agent-gone", "env"),
    ]
    asked = []

    async def _list_keys(_db, agent_id=None):
        asked.append(agent_id)
        return rows

    monkeypatch.setattr(admin_router.agent_key_service, "list_keys", _list_keys)
    monkeypatch.setattr(
        admin_router, "get_agent", lambda agent_id: object() if agent_id == "agent-here" else None
    )

    response = _client(admin_router).get("/admin/agent-keys")
    assert response.status_code == 200, response.text
    body = response.json()
    assert [set(row) for row in body] == [ROW_FIELDS] * 3
    assert [(r["agent_id"], r["registered"], r["rotatable"]) for r in body] == [
        ("agent-here", True, True),
        ("agent-here", True, True),
        ("agent-gone", False, False),
    ]
    assert body[0]["issued_by"] == str(issuer)
    assert body[1]["role"] == "previous"
    assert asked == [None]

    _client(admin_router).get("/admin/agent-keys", params={"agent_id": "agent-here"})
    assert asked == [None, "agent-here"]


def test_issue_and_rotate_answer_their_schemas(monkeypatch):
    audits = []

    async def _issue(_db, agent_id, _issued_by):
        return IssuedKey(agent_id=agent_id, value="lr_agent_first-value", prefix="first-va")

    async def _rotate(_db, agent_id, _issued_by, grace_hours):
        return IssuedKey(agent_id=agent_id, value="lr_agent_second-value", prefix="second-v")

    async def _log_audit(*args, **_kwargs):
        audits.append(args[5])

    monkeypatch.setattr(admin_router.agent_key_service, "issue", _issue)
    monkeypatch.setattr(admin_router.agent_key_service, "rotate", _rotate)
    monkeypatch.setattr(admin_router, "log_audit", _log_audit)
    client = _client(admin_router)

    issued = client.post("/admin/agent-keys/agent-here")
    assert issued.status_code == 201, issued.text
    assert issued.json() == {
        "agent_id": "agent-here",
        "key": "lr_agent_first-value",
        "key_prefix": "first-va",
    }

    rotated = client.post("/admin/agent-keys/agent-here/rotate", params={"grace_hours": 1})
    assert rotated.status_code == 200, rotated.text
    assert rotated.json() == {
        "agent_id": "agent-here",
        "key": "lr_agent_second-value",
        "key_prefix": "second-v",
        "grace_hours": 1,
    }
    # The audit rows name the prefix, never the value (D10).
    assert [a["action"] for a in audits] == ["issue", "rotate"]
    assert all("value" not in str(a) for a in audits)


@pytest.mark.parametrize(
    "agent_id", ["Agent-Here", "agent_here", "-agent", "a" * 51, "agent.here"]
)
def test_a_malformed_agent_id_answers_422(monkeypatch, agent_id):
    async def _refuse(*_args, **_kwargs):
        raise AssertionError("a malformed agent id reached the key service")

    for name in ("list_keys", "issue", "rotate", "revoke"):
        monkeypatch.setattr(admin_router.agent_key_service, name, _refuse)
    client = _client(admin_router)

    answers = {
        "issue": client.post(f"/admin/agent-keys/{agent_id}").status_code,
        "rotate": client.post(f"/admin/agent-keys/{agent_id}/rotate").status_code,
        "revoke": client.delete(f"/admin/agent-keys/{agent_id}").status_code,
        "list": client.get("/admin/agent-keys", params={"agent_id": agent_id}).status_code,
    }
    assert answers == {"issue": 422, "rotate": 422, "revoke": 422, "list": 422}


def test_the_agent_id_rule_is_the_manifests():
    """The router's rule is the manifest's own, so an id an agent can
    declare is never refused here and one it cannot is never accepted."""
    assert admin_router.AGENT_ID_PATTERN == manifest_module._ID_PATTERN
    id_field = manifest_module.AgentManifest.model_fields["id"]
    lengths = [getattr(m, "max_length", None) for m in id_field.metadata]
    assert admin_router.AGENT_ID_MAX_LENGTH in lengths
