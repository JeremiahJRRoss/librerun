"""Blueprint B14: keyless pipeline mode.

The CI smoke has to drive a REAL run on a runner with no provider keys,
so only the provider boundary is faked. These pin the contract that
makes that safe: the flag travels the settings path (not a bare env
read, which would silently do nothing in a container), the fixtures
satisfy each step's normalizer without drift, and every fixture says it
is a fixture so a smoke report can't be mistaken for an investigation.
"""
from __future__ import annotations

import uuid

import pytest

import app.config
from app import capabilities as caps_mod
from agents.vita_v1 import stub_llm
from agents.vita_v1.normalizers import (
    normalize_step_0,
    normalize_step_2,
    normalize_step_3,
    normalize_step_7,
    normalize_step_8,
    normalize_step_9,
)


def _facade(grants=("llm",)):
    return caps_mod.for_run(
        run_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        agent_id="probe-agent",
        grants=list(grants),
    )


@pytest.mark.asyncio
async def test_stub_mode_asks_the_gateway_not_this_process(monkeypatch):
    """This replaces a test that pinned the opposite, and that test was
    describing a defect.

    It asserted that `stub_mode()` resolves through *this* process's
    settings — reasonable when the backend owned the flag. But S4a moved
    `LIBRERUN_STUB_LLM` into the gateway's environment, and compose stops
    passing it here, so the backend's copy is its default `False`
    whatever the operator set. A method documented as a statement of
    fact would have told an agent its fixture-backed report was
    provider-backed (Codex P2).

    The gateway decides, so the gateway is asked."""
    from app.services import gateway_client

    calls = []

    async def fake_health(*a, **kw):
        calls.append(1)
        return {"status": "ok", "stub": True}

    monkeypatch.setattr(gateway_client, "health", fake_health)
    # Set the BACKEND's flag the other way: if the answer follows this,
    # the method is reading the process that does not decide.
    monkeypatch.setattr(app.config.settings, "LIBRERUN_STUB_LLM", False)

    caps = _facade()
    assert await caps.llm.stub_mode() is True
    # Answered once per façade: a pipeline naming it in every step must
    # not pay a round trip per step.
    assert await caps.llm.stub_mode() is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_stub_mode_says_nothing_when_the_gateway_cannot_be_reached(monkeypatch):
    """Claiming "this came from fixtures" on no evidence is the failure
    this method exists to avoid; the call that follows will fail on its
    own and say why."""
    from app.services import gateway_client

    async def unreachable(*a, **kw):
        return None

    monkeypatch.setattr(gateway_client, "health", unreachable)
    assert await _facade().llm.stub_mode() is False


@pytest.mark.asyncio
async def test_stub_mode_needs_the_llm_grant():
    from app.capabilities import CapabilityNotGranted

    caps = _facade(grants=("kb",))
    with pytest.raises(CapabilityNotGranted):
        await caps.llm.stub_mode()


@pytest.mark.asyncio
async def test_the_fixture_is_handed_to_the_gateway_not_returned_here(monkeypatch):
    """Blueprint S4a changed where keyless mode happens, not whose
    fixtures they are. The service used to short-circuit and return the
    fixture itself, which meant a keyless run produced no redaction, no
    span and no cost. It now sends the fixture WITH the call, as
    ``librerun.stub_reply``, and the gateway answers with it — so the
    keyless path exercises the real pipeline and still produces the
    report these fixtures describe."""
    from agents.vita_v1.llm_service import LLMService

    monkeypatch.setattr(app.config.settings, "LIBRERUN_STUB_LLM", True)
    sent: dict = {}

    class _Gateway:
        def stub_mode(self) -> bool:
            return True

        async def complete(self, step, messages, **kwargs):
            sent.update(kwargs)
            reply = kwargs["librerun"]["stub_reply"]
            import json as _json

            return {
                "model": "gpt-4o",
                "choices": [
                    {"message": {"role": "assistant", "content": _json.dumps(reply)}}
                ],
            }

    service = LLMService(_Gateway())
    out = await service.call("refine_problem_statement", [])

    assert sent["librerun"]["stub_reply"] == stub_llm.fixture_for(
        "refine_problem_statement"
    )
    assert out["refined_problem_statement"]
    assert stub_llm.STUB_MARKER in out["refined_problem_statement"]


@pytest.mark.parametrize(
    "step_id,normalizer,probe",
    [
        ("validate_and_classify_inputs", normalize_step_0, "valid"),
        ("refine_problem_statement", normalize_step_2, "refined_problem_statement"),
        ("construct_search_queries_vendor_a", normalize_step_3, "web_queries"),
        ("assess_skills", normalize_step_7, "skills"),
        ("generate_resolution_plan", normalize_step_8, "resolution"),
        ("generate_followup_questions", normalize_step_9, "questions"),
    ],
)
def test_fixtures_pass_their_normalizer_without_drift(step_id, normalizer, probe):
    """A fixture that drifts would make every smoke run log schema drift
    and mask a real regression."""
    out, reports = normalizer(
        stub_llm.fixture_for(step_id),
        step_id=step_id,
        provider="stub",
        model="stub",
    )
    assert not reports, f"{step_id} fixture drifted: {[r.drift_type for r in reports]}"
    assert out[probe], f"{step_id} normalized to an empty {probe}"


def test_prose_fixtures_are_marked_as_fixtures():
    """Every fixture whose text reaches the RENDERED REPORT must announce
    itself, so a smoke artifact can never read as a real investigation.
    (Query/enum fixtures carry no prose and are exempt — they never reach
    the reader.)"""
    import json

    prose_steps = [
        "refine_problem_statement",
        "assess_skills",
        "generate_resolution_plan",
        "generate_followup_questions",
        "generate_log_hints",
    ]
    for step_id in prose_steps:
        # ensure_ascii=False: the marker contains an em-dash, which
        # json.dumps would escape into \u2014 and hide from the check.
        blob = json.dumps(stub_llm.fixture_for(step_id), ensure_ascii=False)
        assert stub_llm.STUB_MARKER in blob, f"{step_id} fixture is unmarked"

    # And the marker survives into the report the smoke asserts on.
    plan = stub_llm.fixture_for("generate_resolution_plan")
    for section in ("mitigation", "resolution", "avoidance"):
        assert stub_llm.STUB_MARKER in plan[section]["text"]


def test_unknown_step_degrades_instead_of_crashing():
    assert stub_llm.fixture_for("a_step_nobody_wrote_a_fixture_for") == {}
    assert not stub_llm.has_fixture("a_step_nobody_wrote_a_fixture_for")
