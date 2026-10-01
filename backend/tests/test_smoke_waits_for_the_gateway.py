"""``librerun_smoke.assert_meta_gateway`` waits for the gateway (issue #85).

``wait_for_health`` returns when the **backend** is healthy, and
`compose.yaml` gives the backend ``gateway: condition: service_started``
**deliberately** — "a run that reaches a model before the gateway is up
fails that phase with a named error, which beats holding the whole API
closed behind it". So the backend answers while the gateway may still be
starting, and the gateway's healthcheck carries ``start_period: 60s``.

Reading ``/api/v1/meta`` once, straight after health, raced that window.
Observed on one commit in CI: a ~29s boot reported ``"ok"`` and a ~4m45s
boot reported ``"unreachable"``, and the job failed on a tree where
nothing was wrong.

The wait is the part that has to be right, so it is tested here against
a fabricated backend rather than only exercised in CI against a live
one — a clean run cannot tell a wait from a lucky read.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parents[2] / "scripts" / "librerun_smoke.py"


def _smoke():
    spec = importlib.util.spec_from_file_location("librerun_smoke_gateway", SMOKE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _meta(gateway, stub_llm=True):
    return {
        "name": "LibreRun",
        "demo": False,
        "stub_llm": stub_llm,
        "gateway": gateway,
        "agents": [{"id": "vita-v1", "name": "VITA"}],
    }


def _backend(module, monkeypatch, replies):
    """A backend that answers ``/meta`` with each reply in turn.

    The last reply repeats, so a test says how the stack behaves rather
    than how many times the code happens to ask.
    """
    seen = []
    slept = []

    def request(method, url, **kwargs):
        seen.append(url)
        reply = replies[min(len(seen) - 1, len(replies) - 1)]
        return reply

    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module.time, "sleep", lambda s: slept.append(s))
    return seen, slept


def test_a_gateway_already_up_is_not_waited_for(monkeypatch):
    module = _smoke()
    seen, slept = _backend(module, monkeypatch, [(200, _meta("ok"))])

    result = module.assert_meta_gateway("http://backend")

    assert result == {"stub_llm": True, "gateway": "ok"}
    assert len(seen) == 1, "a gateway that is up must cost exactly one read"
    assert slept == [], "nothing should sleep when the gateway is already ok"


def test_a_gateway_still_starting_is_waited_for(monkeypatch):
    """The case the fix exists for."""
    module = _smoke()
    seen, slept = _backend(
        module,
        monkeypatch,
        [
            (200, _meta("unreachable", stub_llm=None)),
            (200, _meta("unreachable", stub_llm=None)),
            (200, _meta("ok")),
        ],
    )

    result = module.assert_meta_gateway("http://backend", attempts=60, delay=2.0)

    assert result == {"stub_llm": True, "gateway": "ok"}
    assert len(seen) == 3, "it must keep asking until the gateway answers"
    assert slept == [2.0, 2.0], "it must wait between reads, not spin"


def test_the_single_read_this_replaced_fails_that_very_case(monkeypatch):
    """The injection: one attempt IS the pre-fix code, and it goes red.

    Restoring the old behaviour is exactly ``attempts=1`` — one read, no
    wait — so the fix can be reverted here without touching the file,
    and the case above is proven to be about the wait rather than about
    the fixture.
    """
    module = _smoke()
    seen, slept = _backend(
        module,
        monkeypatch,
        [
            (200, _meta("unreachable", stub_llm=None)),
            (200, _meta("ok")),
        ],
    )

    with pytest.raises(module.SmokeFailure) as caught:
        module.assert_meta_gateway("http://backend", attempts=1, delay=2.0)

    assert "unreachable" in str(caught.value)
    assert len(seen) == 1, "the pre-fix shape reads once — that is the defect"


def test_a_gateway_that_never_comes_up_still_fails(monkeypatch):
    """Waiting must not become never failing."""
    module = _smoke()
    seen, slept = _backend(
        module, monkeypatch, [(200, _meta("unreachable", stub_llm=None))]
    )

    with pytest.raises(module.SmokeFailure) as caught:
        module.assert_meta_gateway("http://backend", attempts=4, delay=1.5)

    message = str(caught.value)
    assert "unreachable" in message
    assert "6s" in message, f"the message must name the bound it waited: {message}"
    assert len(seen) == 4, "it must use every attempt before giving up"


def test_a_non_200_is_immediate_and_not_sat_through(monkeypatch):
    """``wait_for_health`` has already returned, so this is a real failure.

    Retrying it would turn a backend that is refusing requests into a
    two-minute wait and then a message about the gateway, which is not
    what went wrong.
    """
    module = _smoke()
    seen, slept = _backend(module, monkeypatch, [(503, {"detail": "nope"})])

    with pytest.raises(module.SmokeFailure) as caught:
        module.assert_meta_gateway("http://backend")

    assert "503" in str(caught.value)
    assert len(seen) == 1, "a non-200 must not be retried"
    assert slept == []


def test_a_stub_llm_that_is_not_a_fact_still_fails(monkeypatch):
    """The assertion the wait sits in front of is unchanged."""
    module = _smoke()
    _backend(module, monkeypatch, [(200, _meta("ok", stub_llm=None))])

    with pytest.raises(module.SmokeFailure) as caught:
        module.assert_meta_gateway("http://backend")

    assert "stub_llm" in str(caught.value)
