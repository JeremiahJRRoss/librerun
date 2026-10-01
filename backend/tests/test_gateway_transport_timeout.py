"""The backend's HTTP ceiling never fires before the step timeout an
admin configured (blueprint S4a; Codex round 8, P1).

Three timeouts bound one model call, and only two of them are policy:

- the **phase deadline**, ``asyncio.timeout(deadline)`` around the whole
  invocation in ``agent_runner`` — the chassis's backstop;
- the **step timeout**, ``llm.steps[].timeout_seconds`` resolved by the
  gateway and handed to LiteLLM — the admin's choice (L25, D13);
- the **transport ceiling**, the httpx timeout on the call to the
  gateway, which is neither. It exists so a dead socket does not hang a
  worker.

A transport ceiling *below* the step timeout silently overrules the
admin: the shipped ``generate_resolution_plan`` step allows 180s, the
client hung up at 120s, and what came back was
``gateway_unreachable`` — status 0, which ``call_with_retry`` treats as
retryable, so a healthy gateway was reported as down and the provider
call, already billed, was launched twice more.

So the ceiling is derived from the budget the chassis actually enforces
rather than chosen independently, and this is the invariant: no step any
installed agent declares may outlast it.
"""
from __future__ import annotations

import pathlib

import pytest
import yaml

AGENTS = pathlib.Path(__file__).resolve().parents[1] / "agents"


def declared_step_timeouts() -> list[tuple[str, str, int]]:
    """Every ``(agent, step, timeout_seconds)`` on disk."""
    found = []
    for manifest in sorted(AGENTS.glob("*/agent.yaml")):
        data = yaml.safe_load(manifest.read_text()) or {}
        for step in ((data.get("llm") or {}).get("steps") or []):
            seconds = step.get("timeout_seconds")
            if seconds is not None:
                found.append((manifest.parent.name, step.get("id"), int(seconds)))
    return found


def test_there_are_steps_to_check():
    """A scan over nothing proves nothing."""
    assert declared_step_timeouts(), "no agent declares a step timeout"


@pytest.mark.parametrize("agent,step,seconds", declared_step_timeouts())
def test_no_declared_step_outlasts_the_transport_ceiling(agent, step, seconds):
    from app.services import gateway_client

    ceiling = gateway_client.transport_timeout(None)
    assert ceiling >= seconds, (
        f"{agent}/{step} may run {seconds}s but the client hangs up at "
        f"{ceiling}s — the call is killed, billed, reported as "
        f"'gateway_unreachable' and retried"
    )


# --------------------------------------------------------------------------
# The ceiling is the invocation's budget, and it shrinks with it
# --------------------------------------------------------------------------


def test_no_invocation_gets_the_platform_ceiling():
    """A caller with no deadline is bounded by the longest any phase may
    run — never by a number of this module's own, which is what could
    fall below a step's."""
    from app.config import settings
    from app.services import gateway_client

    assert gateway_client.transport_timeout(None) == float(
        settings.LIBRERUN_MAX_PHASE_SECONDS
    )


def test_a_budget_above_the_platform_ceiling_is_clamped():
    """An admin may set ``timeout_seconds`` above the phase ceiling —
    ``LlmStepSpec`` has no upper bound — and a step that outlasts its
    phase is already moot: ``phase_deadline`` clamps the declared
    deadline to the operator's ceiling and the phase is cancelled there.
    The transport ceiling agrees rather than exceeding it."""
    from app.config import settings
    from app.services import gateway_client

    ceiling = float(settings.LIBRERUN_MAX_PHASE_SECONDS)
    assert gateway_client.transport_timeout(ceiling * 10) == ceiling


def test_the_budget_is_never_zero_or_negative():
    """At the deadline the phase is being cancelled anyway; a zero or
    negative httpx timeout is a ValueError, which would replace the
    deadline's own error with a stack trace about arguments."""
    from app.services import gateway_client

    assert gateway_client.transport_timeout(0) >= 1.0
    assert gateway_client.transport_timeout(-5) >= 1.0


@pytest.mark.asyncio
async def test_the_facade_hands_each_call_what_is_left_not_what_it_started_with():
    """The budget is read per call, not snapshotted when the façade was
    built — otherwise the last step of a phase is told it has the whole
    phase left, and the ceiling stops tracking the deadline it exists to
    agree with."""
    import asyncio
    from uuid import uuid4

    from app import capabilities as caps_mod

    caps = caps_mod.for_run(
        run_id=uuid4(),
        tenant_id=uuid4(),
        agent_id="probe-agent",
        grants=["llm"],
        deadline_seconds=60,
    )
    first = caps.seconds_left()
    await asyncio.sleep(0.05)
    second = caps.seconds_left()

    assert first is not None and second is not None
    assert second < first, "the budget did not shrink — it is a snapshot"
    assert second <= 60


@pytest.mark.asyncio
async def test_a_facade_with_no_deadline_reports_no_budget():
    from uuid import uuid4

    from app import capabilities as caps_mod

    caps = caps_mod.for_run(
        run_id=uuid4(), tenant_id=uuid4(), agent_id="probe-agent", grants=["llm"]
    )
    assert caps.seconds_left() is None


@pytest.mark.asyncio
async def test_the_llm_capability_passes_the_budget_to_the_client():
    """The plumbing itself: the façade's budget must reach the call. A
    fix that stops at the client's signature is the round-7 failure
    again — the thing that uses it never sees it."""
    from uuid import uuid4

    from app import capabilities as caps_mod
    from app.services import gateway_client

    seen = {}

    async def fake_chat(*, run_token, step, messages, seconds_left=None, **kwargs):
        seen["seconds_left"] = seconds_left
        return {"choices": []}

    original = gateway_client.chat
    gateway_client.chat = fake_chat
    try:
        caps = caps_mod.for_run(
            run_id=uuid4(),
            tenant_id=uuid4(),
            agent_id="probe-agent",
            grants=["llm"],
            deadline_seconds=180,
        )
        await caps.llm.complete("think", [{"role": "user", "content": "hi"}])
    finally:
        gateway_client.chat = original

    assert seen["seconds_left"] is not None, "the call got no budget at all"
    assert 0 < seen["seconds_left"] <= 180


# --------------------------------------------------------------------------
# A timeout is not an absence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_read_timeout_is_reported_as_a_timeout_not_as_unreachable(monkeypatch):
    """``gateway_unreachable`` at status 0 means "nothing answered". A
    gateway that took the request and ran long is a different event, and
    sending an operator to check a healthy service is the cost of
    conflating them."""
    import httpx

    from app.services import gateway_client

    monkeypatch.setattr(gateway_client, "base_url", lambda: "http://gw.invalid")

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    with pytest.raises(gateway_client.GatewayError) as caught:
        await gateway_client.chat(run_token="t", step="think", messages=[])

    assert caught.value.code == "gateway_timeout"
    assert caught.value.status == 504
    assert caught.value.status != 0


@pytest.mark.asyncio
async def test_a_connect_timeout_is_still_unreachable(monkeypatch):
    """The other half of the distinction: nothing answered the
    handshake, so ``gateway_unreachable`` is the truth."""
    import httpx

    from app.services import gateway_client

    monkeypatch.setattr(gateway_client, "base_url", lambda: "http://gw.invalid")

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    with pytest.raises(gateway_client.GatewayError) as caught:
        await gateway_client.chat(run_token="t", step="think", messages=[])

    assert caught.value.code == "gateway_unreachable"
    assert caught.value.status == 0


@pytest.mark.asyncio
async def test_connecting_keeps_a_short_bound_of_its_own(monkeypatch):
    """An hour is the right budget for a model that thinks for an hour
    and the wrong one for a TCP handshake, so the two are separate."""
    import httpx

    from app.services import gateway_client

    monkeypatch.setattr(gateway_client, "base_url", lambda: "http://gw.invalid")
    seen = {}

    class _Client:
        def __init__(self, *a, timeout=None, **kw):
            seen["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    with pytest.raises(gateway_client.GatewayError):
        await gateway_client.chat(
            run_token="t", step="think", messages=[], seconds_left=3000
        )

    budget = seen["timeout"]
    assert budget.read == 3000.0
    assert budget.connect == gateway_client.CONNECT_TIMEOUT
    assert budget.connect < budget.read
