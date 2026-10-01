"""A tenant's step override obeys the rules a declaration obeys
(blueprint S4a, L25).

The manifest's ``llm.steps[]`` is validated when the agent is loaded.
The admin's overrides go into ``agent_step_configs`` and the gateway
gives them PRECEDENCE over the manifest, so an unusable value there is
worse than an unusable value in the manifest: the agent still loads,
the row is still read, and every invocation of that step fails until
somebody finds the row. Codex found `max_tokens: 0` and a negative
temperature going straight into the table.

The check is not a second copy of the rules — the values go through
``LlmStepSpec`` itself, so the two cannot drift.
"""
from __future__ import annotations

import pytest

from app.services import agent_step_config_service as svc


@pytest.mark.parametrize(
    "values, expect",
    [
        ({"max_tokens": 0}, "max_tokens"),
        ({"max_tokens": -1}, "max_tokens"),
        ({"max_tokens": 1.5}, "max_tokens"),
        ({"max_tokens": "many"}, "max_tokens"),
        ({"temperature": -0.1}, "temperature"),
        ({"temperature": "warm"}, "temperature"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": -30}, "timeout_seconds"),
        ({"provider": "p" * 51}, "provider"),
        ({"model": "m" * 201}, "model"),
    ],
)
def test_an_override_the_manifest_would_reject_is_refused(values, expect):
    with pytest.raises(svc.InvalidStepOverride) as exc:
        svc.validate_override("analyze", values)
    assert exc.value.step_id == "analyze"
    assert expect in str(exc.value)


@pytest.mark.parametrize(
    "values",
    [
        {"model": "gpt-4o-mini"},
        {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        {"temperature": 0.0},
        {"temperature": 2.0},
        {"max_tokens": 1},
        {"timeout_seconds": 1},
        {},
    ],
)
def test_a_value_a_declaration_may_carry_is_accepted(values):
    """No stricter than the manifest. A check that refused a value an
    agent may legally declare would be a second, invisible vocabulary —
    and it would be removed the first time it fired."""
    svc.validate_override("analyze", values)


def test_clearing_a_field_is_not_validated_as_a_value():
    """``null`` returns the step to the manifest's default, so it is a
    clear rather than a setting, and `max_tokens: None` must not read as
    `max_tokens: 0`."""
    svc.validate_override("analyze", {"max_tokens": None, "temperature": None})
