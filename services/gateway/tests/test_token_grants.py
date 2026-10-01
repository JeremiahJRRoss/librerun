"""A credential carries the authority it was issued with.

`Principal.token_grants` was loaded from the run token record and then
never read: `grants()` consulted the current manifest snapshot alone. So
a token minted for an agent that granted nothing could begin calling
models the moment someone added `llm` to the manifest — across a
restart, say, where token cleanup is best-effort (Codex round 17, P1).

The two directions are asymmetric on purpose:

- a REMOVAL takes effect at once, because an operator who takes `llm`
  out of a manifest has withdrawn it and a token minted an hour ago must
  not go on spending;
- an ADDITION does not reach backwards.

An agent key alone carries no invocation, so there is no
invocation-time grant to consult and the snapshot decides — which is
what `GET /v1/models` needs.
"""
from __future__ import annotations

import pytest

from gateway import errors
from gateway.auth import Principal
from gateway.snapshots import Snapshot


def principal(*, manifest_grants, token_grants, with_run=True) -> Principal:
    return Principal(
        agent_id="demo-v1",
        snapshot=Snapshot(
            agent_id="demo-v1",
            manifest={"capabilities": list(manifest_grants)},
        ),
        run_id="r" if with_run else None,
        tenant_id="t" if with_run else None,
        token_grants=list(token_grants),
    )


def test_a_manifest_addition_does_not_escalate_a_live_token():
    """The defect, as the invariant it violates."""
    p = principal(manifest_grants=["llm", "kb"], token_grants=["kb"])
    assert p.grants("kb") is True
    assert p.grants("llm") is False


def test_a_manifest_removal_still_takes_effect_immediately():
    """The other direction must not be lost to the fix: a token minted
    when `llm` was granted stops working when it is withdrawn."""
    p = principal(manifest_grants=["kb"], token_grants=["llm", "kb"])
    assert p.grants("llm") is False


def test_a_capability_granted_in_both_places_works():
    p = principal(manifest_grants=["llm"], token_grants=["llm"])
    assert p.grants("llm") is True


def test_an_agent_key_alone_is_judged_by_the_manifest():
    """`GET /v1/models` presents no run token, so there is no
    invocation-time grant to require — requiring one would refuse the
    one endpoint an agent key is for."""
    p = principal(manifest_grants=["llm"], token_grants=[], with_run=False)
    assert p.grants("llm") is True


def test_the_refusal_says_which_of_the_two_said_no():
    """An operator sent to check a manifest that DOES grant the
    capability has been told the wrong thing."""
    late = principal(manifest_grants=["llm"], token_grants=[])
    with pytest.raises(errors.GatewayError) as caught:
        late.require("llm")
    assert "minted before" in caught.value.message

    never = principal(manifest_grants=[], token_grants=[])
    with pytest.raises(errors.GatewayError) as caught:
        never.require("llm")
    assert "does not grant" in caught.value.message


def test_require_and_grants_agree_in_both_directions():
    """Two entry points to one rule; a fix applied to one of them is the
    shape this batch keeps finding."""
    cases = [
        (["llm"], ["llm"], True),
        (["llm"], [], False),
        ([], ["llm"], False),
        ([], [], False),
    ]
    for manifest, token, expected in cases:
        p = principal(manifest_grants=manifest, token_grants=token)
        assert p.grants("llm") is expected
        if expected:
            p.require("llm")
        else:
            with pytest.raises(errors.GatewayError):
                p.require("llm")
