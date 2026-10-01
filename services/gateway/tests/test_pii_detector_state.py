"""Blueprint S4c (gap H15): the detector's state is this process's too.

The gateway image ships ``backend/app`` and ``gateway/redaction.py``
runs the chassis's own ``pii_service`` (see the Dockerfile), so outbound
redaction has exactly the failure mode intake has: Presidio's
named-entity stage is the only stage that finds a person, and when it
cannot run the regex stages alone would send that person's name to
somebody else's model.

Which means "is the detector ready" is a question about THIS process and
cannot be answered by asking the backend. These tests pin the three
pieces that make it answerable here: the lifespan warms it, ``/healthz``
reports it, and a request that would have been redacted is refused with
``pii_detector_unavailable`` rather than forwarded.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.services import pii_service

from gateway import errors, main as gateway_main
from gateway.redaction import redact_request

# A person and a place: the classes ONLY stage 3 finds. An email would
# be caught by the regex identifier rule and would pass with stage 3
# switched off — proving nothing about this batch.
PERSON = "Marguerite Okonkwo"


@contextmanager
def constructor_raises():
    """The missing spaCy model, injected the way a deployment has it."""
    import presidio_analyzer

    saved = (
        pii_service._analyzer,
        pii_service._anonymizer,
        pii_service._state,
        pii_service._state_error,
    )
    pii_service._analyzer = None
    pii_service._anonymizer = None
    pii_service._state = None
    pii_service._state_error = None
    try:
        with patch.object(
            presidio_analyzer,
            "AnalyzerEngine",
            side_effect=OSError("injected: no spaCy model"),
        ):
            yield
    finally:
        (
            pii_service._analyzer,
            pii_service._anonymizer,
            pii_service._state,
            pii_service._state_error,
        ) = saved
        pii_service._notice_at.clear()
        pii_service._pending_audits.clear()


def _chat() -> dict:
    return {
        "model": "librerun/think",
        "messages": [{"role": "user", "content": f"{PERSON} filed the ticket"}],
    }


def test_the_baseline_detector_is_ready_here_too():
    """This process must be able to reach ``ready`` at all, or every
    refusal below is indistinguishable from a broken environment."""
    status = pii_service.warm_detector(force=True)
    if status.state != pii_service.READY:
        pytest.fail(
            "the gateway's own image installs en_core_web_lg; this runner "
            f"reports state={status.state!r} (error={status.error!r}). Run "
            "`python -m spacy download en_core_web_lg`."
        )
    outbound, _ = redact_request(
        _chat(), tenant_id="11111111-1111-1111-1111-111111111111", enabled=True
    )
    assert PERSON not in str(outbound), "the NER stage did not fire"


def test_outbound_redaction_refuses_rather_than_forwarding_a_half_walked_request():
    with constructor_raises():
        with pytest.raises(pii_service.PiiDetectorUnavailable) as excinfo:
            redact_request(
                _chat(),
                tenant_id="11111111-1111-1111-1111-111111111111",
                enabled=True,
            )
    assert PERSON not in str(excinfo.value)


def test_redaction_switched_off_is_not_affected():
    """An agent that opted out of outbound redaction was never having
    its prompt walked, so there is nothing for a broken detector to
    refuse. Refusing here would take a working deployment offline over a
    stage it does not use."""
    with constructor_raises():
        outbound, report = redact_request(
            _chat(),
            tenant_id="11111111-1111-1111-1111-111111111111",
            enabled=False,
        )
    assert PERSON in str(outbound)


@pytest.mark.asyncio
async def test_healthz_reports_the_detector():
    pii_service.warm_detector(force=True)
    body = await gateway_main.healthz()
    assert body["pii_detector"] == {"state": "ready", "coverage": "ner"}

    with constructor_raises():
        body = await gateway_main.healthz()
    assert body["pii_detector"] == {"state": "unavailable", "coverage": "regex_only"}
    # Two fields and no more: this endpoint is unauthenticated and every
    # agent container on the network can reach it, so the exception class
    # the backend's /health carries does not belong here.
    assert set(body["pii_detector"]) == {"state", "coverage"}


@pytest.mark.asyncio
async def test_the_refusal_is_the_code_the_catalogue_documents():
    """The handler spells the code as a literal so
    ``test_refusal_catalogue.py`` can see it; this is what keeps that
    literal equal to the chassis constant it stands for."""
    response = await gateway_main._pii_detector_unavailable(
        None,
        pii_service.PiiDetectorUnavailable(
            pii_service.UNAVAILABLE, stage="outbound_redaction", error="OSError"
        ),
    )
    assert response.status_code == 503
    import json

    body = json.loads(bytes(response.body))
    assert body["error"]["code"] == pii_service.UNAVAILABLE_CODE
    assert body["error"]["type"] == "service_unavailable"
    assert PERSON not in json.dumps(body)


def test_the_unavailable_factory_builds_a_503():
    refusal = errors.unavailable("probe_code", "probe message")
    assert refusal.status_code == 503
    assert refusal.body()["error"] == {
        "message": "probe message",
        "type": "service_unavailable",
        "code": "probe_code",
    }


def test_the_lifespan_warms_the_detector():
    """Warmed at startup, not on the first model call: the source is the
    lifespan's own text, because a test that calls ``warm_detector``
    itself would pass with the line deleted."""
    import inspect

    source = inspect.getsource(gateway_main.lifespan)
    assert "pii_service.warm_detector()" in source, (
        "the gateway lifespan must warm the PII detector, or /healthz "
        "reports a state nothing measured"
    )
