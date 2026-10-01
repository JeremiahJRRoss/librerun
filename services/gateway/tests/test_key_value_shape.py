"""An agent key has a shape, and the shape is load-bearing.

`LIBRERUN_AGENT_KEY_<ID>` becomes the container's `OPENAI_API_KEY`. The
obvious operator mistake is therefore to paste a PROVIDER key into it —
and the `lr_agent_` format exists so that mistake fails loudly at `up`.
It did not: `parse_key_variables` accepted any non-empty value,
reconciliation registered its hash, and the gateway then honoured a real
provider key as that agent's credential while compose handed the same
live credential to a container that (with `network.egress`) could spend
it directly (Codex P1).

Both ends are checked here. Refusing at reconciliation is the fix;
refusing at lookup is what makes a row written before the fix stop
working rather than linger.
"""
from __future__ import annotations

import pytest

from gateway import keys
from gateway.config import KEY_VALUE_PREFIX

# Shaped like the real thing, and obviously not a real one.
PROVIDER_KEY = "sk-proj-000000000000000000000000000000000000000000000000"


def _env(**pairs) -> dict:
    return {f"LIBRERUN_AGENT_KEY_{k}": v for k, v in pairs.items()}


def test_a_provider_key_in_an_agent_key_variable_is_refused():
    with pytest.raises(keys.KeyVariableError) as caught:
        keys.parse_key_variables(_env(ECHO_V1=PROVIDER_KEY))
    assert "LIBRERUN_AGENT_KEY_ECHO_V1" in str(caught.value)
    assert KEY_VALUE_PREFIX in str(caught.value)


def test_the_refusal_never_echoes_the_value():
    """It may BE the provider key. Every refusal in this service names a
    path and not a value, and this is the one where it matters most."""
    with pytest.raises(keys.KeyVariableError) as caught:
        keys.parse_key_variables(_env(ECHO_V1=PROVIDER_KEY))
    assert PROVIDER_KEY not in str(caught.value)
    assert "sk-proj" not in str(caught.value)


def test_the_outgoing_value_of_a_rotation_is_checked_too():
    """A rotation that moves a provider key into _PREVIOUS keeps it
    usable for the whole window, which is the worse half of the bug."""
    env = _env(ECHO_V1=keys.mint_key())
    env["LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS"] = PROVIDER_KEY
    with pytest.raises(keys.KeyVariableError) as caught:
        keys.parse_key_variables(env)
    assert "LIBRERUN_AGENT_KEY_ECHO_V1_PREVIOUS" in str(caught.value)


@pytest.mark.parametrize(
    "value",
    [
        "",                         # dropped as blank before parsing
        "   ",                      # the same after stripping
    ],
)
def test_a_blank_variable_is_not_a_refusal_it_is_absent(value):
    """The prefix check must not turn "unset" into "malformed" — an
    operator who has not provisioned an agent yet gets compose's own
    `:?` message, which names what to do."""
    assert keys.parse_key_variables(_env(ECHO_V1=value)) == []


def test_a_properly_shaped_key_still_parses():
    minted = keys.mint_key()
    entries = keys.parse_key_variables(_env(ECHO_V1=minted))
    assert [(e.agent_id, e.value, e.role) for e in entries] == [
        ("echo-v1", minted, "current")
    ]


def test_what_mint_produces_is_what_the_check_accepts():
    """The two ends of the format, pinned against each other rather than
    against a literal typed twice."""
    assert keys.mint_key().startswith(KEY_VALUE_PREFIX)


@pytest.mark.asyncio
async def test_lookup_refuses_a_bearer_that_is_not_shaped_like_a_key():
    """Without querying: a value that cannot be an agent key is not one,
    whatever rows exist. The fake session fails the test if it is used."""

    class _NeverQueried:
        async def execute(self, *_a, **_kw):  # pragma: no cover - the point
            raise AssertionError(
                "resolve() queried the database for a value that cannot be "
                "a LibreRun agent key"
            )

    assert await keys.resolve(_NeverQueried(), PROVIDER_KEY) is None
