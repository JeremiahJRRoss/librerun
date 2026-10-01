"""The container client's ceiling is the invocation's budget too
(blueprint S4a; Codex round 8, P1).

The SDK carried the same flat 120s the backend did, and the same
consequence: a step an admin configured for longer was cut off by the
transport, the provider billed for it, and the agent was told
``gateway_unreachable`` about a gateway that was answering. The
invocation already knows its budget — ``Invocation.seconds_left()``,
used by ``_server`` to bound the handler — so the client asks it rather
than holding a number of its own.
"""
from __future__ import annotations

import time

import pytest

from librerun_agent import _llm


class _Inv:
    """The parts of an Invocation this client reads."""

    def __init__(self, deadline_seconds, started_at=None):
        self.deadline_seconds = deadline_seconds
        self.started_at = started_at if started_at is not None else time.monotonic()
        self.token = "tok"
        self.traceparent = None
        self.tracestate = None

    def seconds_left(self):
        if self.deadline_seconds is None:
            return None
        return max(0.0, self.started_at + self.deadline_seconds - time.monotonic())


def test_the_ceiling_comes_from_the_invocations_budget():
    llm = _llm.Llm(_Inv(180))
    assert 170 < llm._timeout() <= 180


def test_a_step_longer_than_the_old_constant_is_no_longer_cut_off():
    """The shipped `generate_resolution_plan` allows 180s. Under the old
    flat 120 the client hung up first; the budget is what decides now."""
    llm = _llm.Llm(_Inv(300))
    assert llm._timeout() >= 180


def test_no_invocation_deadline_falls_back_to_the_default_ceiling():
    """With nothing to go on, the platform's DEFAULT ceiling is the only
    bound available — a guess, and the only place the constant is used."""
    llm = _llm.Llm(_Inv(None))
    assert llm._timeout() == _llm.DEFAULT_MAX_PHASE_SECONDS


def test_a_deadline_above_the_default_ceiling_is_honoured_not_clamped():
    """This replaces a test that asserted the opposite, and it was wrong.

    `LIBRERUN_MAX_PHASE_SECONDS` is configurable and
    `phases[].deadline_seconds` has no maximum, so a deployment may
    legitimately run a 5400-second step under a 7200-second ceiling. The
    container cannot see either value — it sees the deadline it was
    given, which is the resolved policy. Clamping it to this module's
    constant disconnected such a call at 3600s, abandoned billed work and
    reported `gateway_timeout` for it (Codex P2): the same mistake as the
    ceiling itself, a bound applied by something that does not own the
    policy."""
    generous = _llm.DEFAULT_MAX_PHASE_SECONDS * 2
    llm = _llm.Llm(_Inv(generous))

    assert llm._timeout() > _llm.DEFAULT_MAX_PHASE_SECONDS
    assert llm._timeout() <= generous


def test_the_constant_is_only_a_fallback_and_says_so():
    """Named for what it is: the DEFAULT of a configurable ceiling, not
    a ceiling this process may enforce."""
    assert not hasattr(_llm, "MAX_PHASE_SECONDS"), (
        "the old name is back; it read as a limit this module owns"
    )
    assert _llm.DEFAULT_MAX_PHASE_SECONDS == 3600.0


def test_an_exhausted_budget_is_still_a_usable_timeout():
    """urllib raises on a zero or negative timeout; the deadline is
    cancelling the handler at this point anyway."""
    llm = _llm.Llm(_Inv(60, started_at=time.monotonic() - 600))
    assert llm._timeout() >= 1.0


def test_the_module_holds_no_fixed_call_timeout():
    """The constant that caused this is gone rather than renamed: only
    the platform ceiling remains, and it is a documented fallback."""
    assert not hasattr(_llm, "DEFAULT_TIMEOUT")


def test_a_timeout_is_not_reported_as_an_unreachable_gateway(monkeypatch):
    import urllib.error
    import urllib.request

    def _raise(*a, **kw):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    monkeypatch.setenv("LIBRERUN_GATEWAY_URL", "http://gw.invalid")

    llm = _llm.Llm(_Inv(180))
    with pytest.raises(_llm.LlmError) as caught:
        llm._post("/v1/chat/completions", {}, "think", 5.0)

    assert caught.value.code == "gateway_timeout"
    assert caught.value.status == 504


def test_a_real_connection_failure_is_still_unreachable(monkeypatch):
    import urllib.error
    import urllib.request

    def _raise(*a, **kw):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", _raise)
    monkeypatch.setenv("LIBRERUN_GATEWAY_URL", "http://gw.invalid")

    llm = _llm.Llm(_Inv(180))
    with pytest.raises(_llm.LlmError) as caught:
        llm._post("/v1/chat/completions", {}, "think", 5.0)

    assert caught.value.code == "gateway_unreachable"
    assert caught.value.status == 0


# --- and the escape hatch that undid all of it -------------------------


def test_a_per_call_timeout_is_refused_rather_than_honoured():
    """`complete()` used to take a `timeout` that REPLACED the budget
    above, which handed every caller the exact failure this whole module
    exists to prevent: a client timeout shorter than the step's closes
    the socket while the gateway goes on running the provider call and
    billing it, and the agent is told `gateway_timeout` about work that
    is still happening (Codex P2).

    Refused, not ignored. An author who writes `timeout=5` believes
    something about the call, and a parameter that quietly does nothing
    is the same defect one door along."""
    import asyncio

    llm = _llm.Llm(_Inv(30))
    with pytest.raises(ValueError) as caught:
        asyncio.run(llm.complete("draft", [], timeout=5))
    message = str(caught.value)
    assert "timeout" in message
    # It must say WHOSE the timeout is and where to change it, or the
    # author has been refused without being told what to do instead.
    assert "admin" in message


def test_the_refusal_does_not_reach_the_network():
    """The check runs before anything is sent: a refused call must not
    have already started the provider work it is complaining about."""
    import asyncio

    calls = []
    llm = _llm.Llm(_Inv(30))
    llm._post = lambda *a, **k: calls.append(a)  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        asyncio.run(llm.complete("draft", [], timeout=1))
    assert calls == []


def test_an_ordinary_call_still_uses_the_invocations_budget(monkeypatch):
    """The fix must not cost the thing it protects."""
    import asyncio

    seen = {}

    def fake_post(path, payload, step, timeout):
        seen["timeout"] = timeout
        return {"choices": []}

    llm = _llm.Llm(_Inv(300))
    llm._post = fake_post  # type: ignore[method-assign]
    asyncio.run(llm.complete("draft", [{"role": "user", "content": "hi"}]))
    assert 290 < seen["timeout"] <= 300
