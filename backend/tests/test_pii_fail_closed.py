"""Blueprint S4c (gap H15): the detector fails closed.

Stage 3 of ``pii_service.redact`` — Presidio's named-entity pass — is
the only stage that finds a person, a place or an organisation. Until
S4c, a missing spaCy model or a raising ``analyze`` made
``_apply_presidio`` return its input and ``redact`` carry on through the
regex stages, so those classes disappeared from every persist and export
point with nothing said anywhere. The demo agent's requirements (P-10) chose that;
CLAUDE.md's "unredacted content never touches the database" cannot
coexist with it.

Every test here INJECTS the failure rather than describing it, in the
two shapes a deployment actually produces:

* the analyzer **constructor** raises — the missing spaCy model, which
  leaves the detector ``unavailable``;
* **analyze** raises on the fixture text — a runtime fault, which leaves
  the detector ``failed`` with a count.

and then asserts what each attach point does about it: 503 at intake,
the preview and the upload; a refusal at the run boundary and the MCP
``redact`` tool; a stripped span and a dropped record on the way out.
The opt-out (``LIBRERUN_PII_ALLOW_DEGRADED=true``) is asserted to
restore the old behaviour WITH the stamp and the audit row.

The last test is the negative probe: with the policy removed, the
fixture's name reaches a database dump. It is the only one that proves
the rest are load-bearing.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from starlette.testclient import TestClient

from app.agents.protocol import AgentProtocol, AnalysisResult, InvestigationResult
from app.database import get_db
from app.middleware import get_current_user
from app.observability import otlp_walk, walkers
from app.services import pii_service, run_boundary

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The fixture identity. A PERSON and a PLACE, because those are exactly
# the classes the regex stages cannot see: an assertion built on an
# email or an IP address would pass with stage 3 switched off and prove
# nothing at all about this batch.
FIXTURE_NAME = "Marguerite Okonkwo"
FIXTURE_PLACE = "Ouagadougou"
FIXTURE_TEXT = (
    f"{FIXTURE_NAME} in {FIXTURE_PLACE} reported the handshake failure."
)

REQUIRE_DB = os.environ.get("LIBRERUN_REQUIRE_DB", "").strip().lower() in {
    "1",
    "true",
    "yes",
}


def _detector_or_fail():
    """A working detector, or a failure naming the fix.

    Not a skip. Half of this module injects a BROKEN detector, and a
    broken detector is indistinguishable from an absent one — so on a
    runner with no spaCy model those tests would "pass" against the
    wrong baseline while the control cases silently vanished. Every CI
    job that runs this module installs the model (unit-suites,
    chassis-zero-agents, adapter-battery), the same one
    ``backend/Dockerfile`` installs.
    """
    pii_service.warm_detector(force=True)
    status = pii_service.detector_status()
    if status.state != pii_service.READY:
        pytest.fail(
            "this module needs a WORKING PII detector as its baseline and "
            f"found state={status.state!r} (error={status.error!r}). Run "
            "`python -m spacy download en_core_web_lg`. It may not skip: a "
            "broken detector looks exactly like the failures these tests "
            "inject."
        )
    return status


@pytest.fixture(scope="module", autouse=True)
def _detector_baseline():
    return _detector_or_fail()


@pytest.fixture(autouse=True)
def _detector_isolation():
    """Put the module globals back after every test.

    ``pii_service`` caches the engine, the state and the counters in
    module globals on purpose (building an ``AnalyzerEngine`` costs
    seconds). Saving and restoring them is cheaper than re-warming, and
    it stops one test's injected fault from becoming the next test's
    baseline.
    """
    saved = (
        pii_service._analyzer,
        pii_service._anonymizer,
        pii_service._state,
        pii_service._state_error,
        pii_service._call_failures,
        pii_service._degraded_calls,
    )
    yield
    (
        pii_service._analyzer,
        pii_service._anonymizer,
        pii_service._state,
        pii_service._state_error,
        pii_service._call_failures,
        pii_service._degraded_calls,
    ) = saved
    pii_service._notice_at.clear()
    pii_service._pending_audits.clear()
    pii_service._notice_guard.busy = False


# --------------------------------------------------------------------------
# The two injections
# --------------------------------------------------------------------------


@contextmanager
def constructor_raises(exc: BaseException | None = None):
    """The missing spaCy model: ``AnalyzerEngine()`` raises."""
    import presidio_analyzer

    pii_service._analyzer = None
    pii_service._anonymizer = None
    pii_service._state = None
    pii_service._state_error = None
    boom = exc or OSError("injected: [E050] Can't find model 'en_core_web_lg'")
    with patch.object(presidio_analyzer, "AnalyzerEngine", side_effect=boom):
        yield


@contextmanager
def analyze_raises(exc: BaseException | None = None):
    """A runtime fault: the engine exists and ``analyze`` raises."""
    analyzer, _ = pii_service._get_presidio()
    assert analyzer is not None, "the baseline detector must exist for this shape"
    pii_service._state = pii_service.READY
    pii_service._state_error = None
    pii_service._call_failures = 0
    with patch.object(analyzer, "analyze", side_effect=exc or RuntimeError("injected")):
        yield


@contextmanager
def analyze_finds_nothing():
    """The third shape, and the quiet one: the engine builds, ``analyze``
    returns, and it returns nothing.

    A registry that ended up with no recognizers for this language, or a
    spaCy pipeline loaded without its ``ner`` component, does exactly
    this — every call succeeds and no name is ever removed. Neither
    injection above produces it: one stops the constructor and the other
    raises inside the call, so both leave a trace the state already
    reports. This one leaves none, which is what makes it worth a test.
    """
    analyzer, _ = pii_service._get_presidio()
    assert analyzer is not None, "the baseline detector must exist for this shape"
    pii_service._state = None
    pii_service._state_error = None
    pii_service._call_failures = 0
    with patch.object(analyzer, "analyze", return_value=[]):
        yield


@contextmanager
def opt_out(monkeypatch):
    monkeypatch.setattr(
        pii_service.settings, "LIBRERUN_PII_ALLOW_DEGRADED", True, raising=False
    )
    yield


# --------------------------------------------------------------------------
# The readiness state
# --------------------------------------------------------------------------


def test_a_warmed_detector_is_ready_with_ner_coverage():
    status = pii_service.warm_detector(force=True)
    assert status.state == pii_service.READY
    assert status.coverage == pii_service.COVERAGE_NER
    assert status.error is None
    # The baseline the rest of the module rests on: stage 3 really runs.
    redacted, applied = pii_service.redact(FIXTURE_TEXT)
    assert FIXTURE_NAME not in redacted
    assert "PERSON" in {r.pii_type for r in applied}


def test_a_probe_that_finds_nothing_is_unavailable_not_ready():
    """The warm-up's own negative test.

    ``warm_detector``'s docstring says it proves the detector answers,
    and the probe text is chosen to carry a person and a place so that
    "it answered" means "stage 3 ran". For that to be true the RESULT
    has to be looked at: an engine that returns an empty list answers
    every call and removes no name, and a warm-up that only caught
    exceptions called it ``ready`` — the gate that reports success by
    not looking, on the one state the whole module's policy reads.
    """
    with analyze_finds_nothing():
        status = pii_service.warm_detector()

    assert status.state == pii_service.UNAVAILABLE, (
        "an analyzer that recognises nothing was reported as a working "
        "named-entity stage"
    )
    assert status.coverage == pii_service.COVERAGE_REGEX_ONLY
    assert status.error == "DetectorProbeFoundNothing"
    # ``unavailable``, not ``failed``: nothing raised, so there is no
    # call to count.
    assert status.failures == 0


def test_that_state_makes_the_chassis_refuse_like_any_other():
    """The reason the state matters: it is the same ``unavailable`` every
    attach point already reads, so a silently blind engine now refuses
    rather than persisting text it did not redact."""
    with analyze_finds_nothing():
        pii_service.warm_detector()
        with pytest.raises(pii_service.PiiDetectorUnavailable) as caught:
            pii_service.redact(FIXTURE_TEXT)

    assert caught.value.state == pii_service.UNAVAILABLE
    assert FIXTURE_NAME not in str(caught.value)


def test_a_probe_that_finds_something_is_still_ready():
    """The positive control. Without it the check above is satisfied by a
    warm-up that can never say ``ready`` at all — and every other test in
    this module rests on the real detector reaching that state."""
    analyzer, _ = pii_service._get_presidio()
    assert analyzer is not None
    pii_service._state = None
    with patch.object(
        analyzer,
        "analyze",
        return_value=[SimpleNamespace(entity_type="PERSON", score=0.9)],
    ):
        status = pii_service.warm_detector()

    assert status.state == pii_service.READY
    assert status.coverage == pii_service.COVERAGE_NER


def test_the_probe_text_is_what_the_check_rests_on():
    """The demand is "at least one entity", so the probe text has to be
    text a named-entity model finds something in. Asserted against the
    REAL engine rather than against the sentence, because a future
    rewording that reads well and recognises as nothing would make the
    check vacuous while every test above still passed."""
    analyzer, _ = pii_service._get_presidio()
    assert analyzer is not None
    found = analyzer.analyze(text=pii_service._PROBE_TEXT, language="en")
    assert len(found) >= pii_service._PROBE_MIN_ENTITIES
    # And it is a fixture, not anybody's data: nothing in it appears in
    # the module's other constants or in the placeholders it emits.
    assert "@" not in pii_service._PROBE_TEXT


def test_a_constructor_that_raises_leaves_the_detector_unavailable_with_its_class():
    with constructor_raises(ValueError("injected")):
        status = pii_service.warm_detector()
        assert status.state == pii_service.UNAVAILABLE
        assert status.coverage == pii_service.COVERAGE_REGEX_ONLY
        # The CLASS, never the message: a Presidio error can quote a path.
        assert status.error == "ValueError"
        assert "injected" not in json.dumps(status.as_dict())


def test_an_analyze_that_raises_leaves_the_detector_failed_with_a_count():
    with analyze_raises():
        for _ in range(3):
            with pytest.raises(pii_service.PiiDetectorUnavailable):
                pii_service.redact(FIXTURE_TEXT)
        status = pii_service.detector_status()
    assert status.state == pii_service.FAILED
    assert status.coverage == pii_service.COVERAGE_REGEX_ONLY
    assert status.error == "RuntimeError"
    assert status.failures == 3, "every failing call is counted, not just the first"


def test_a_detector_that_answers_again_is_ready_again_and_keeps_the_count():
    """A transient fault must not need a restart to clear.

    Fail-closed is a property of each CALL — every raising call above
    refused — not a latch that turns one hiccup into an outage only an
    operator can end. The count survives so /health still shows it
    happened.
    """
    with analyze_raises():
        with pytest.raises(pii_service.PiiDetectorUnavailable):
            pii_service.redact(FIXTURE_TEXT)
    assert pii_service.detector_status().state == pii_service.FAILED
    redacted, _ = pii_service.redact(FIXTURE_TEXT)
    assert FIXTURE_NAME not in redacted
    status = pii_service.detector_status()
    assert status.state == pii_service.READY
    assert status.failures == 1, "recovery clears the state, not the history"


def test_a_process_that_never_warmed_is_never_reported_ready():
    """/health must not assume. With nothing warmed and a constructor
    that raises, the status is determined on the spot and is honest."""
    with constructor_raises():
        assert pii_service._state is None
        assert pii_service.detector_status().state == pii_service.UNAVAILABLE


def test_a_live_engine_with_a_cleared_state_is_determined_ready():
    """The state and the cached engine are two globals, and they can be
    reset apart. When they are, the ENGINE is the ground truth: reading
    ``unavailable`` off a detector that works would refuse every request
    on the strength of a bookkeeping gap.
    """
    pii_service.warm_detector(force=True)
    assert pii_service._analyzer not in (None, False)
    pii_service._state = None  # the engine survives; only the state went
    assert pii_service.detector_status().state == pii_service.READY
    redacted, _ = pii_service.redact(FIXTURE_TEXT)
    assert FIXTURE_NAME not in redacted


def test_the_refusal_names_the_state_and_the_stage_and_no_text():
    with constructor_raises():
        with pytest.raises(pii_service.PiiDetectorUnavailable) as excinfo:
            pii_service.redact(FIXTURE_TEXT, stage="intake")
    message = str(excinfo.value)
    assert excinfo.value.code == "pii_detector_unavailable"
    assert excinfo.value.stage == "intake"
    assert excinfo.value.state == pii_service.UNAVAILABLE
    assert FIXTURE_NAME not in message and FIXTURE_PLACE not in message


# --------------------------------------------------------------------------
# /health and the boot
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_reports_the_detector_state_and_coverage():
    from app import main

    pii_service.warm_detector(force=True)
    body = await main.health()
    assert body["pii_detector"]["state"] == "ready"
    assert body["pii_detector"]["coverage"] == "ner"

    with analyze_raises():
        with pytest.raises(pii_service.PiiDetectorUnavailable):
            pii_service.redact(FIXTURE_TEXT)
        body = await main.health()
    assert body["pii_detector"]["state"] == "failed"
    assert body["pii_detector"]["coverage"] == "regex_only"
    assert body["pii_detector"]["failures"] == 1


def test_the_chassis_boots_through_its_lifespan_with_an_unavailable_detector():
    """An unavailable detector is a state to REPORT, not a boot failure.

    A chassis that refuses to start says nothing to anyone; one that
    starts and answers /health with ``unavailable`` is what an operator
    can act on — and what every endpoint then refuses on. Driven in a
    subprocess through the real lifespan, because the claim is about
    ``app.main``'s startup and not about a function called by hand.
    """
    probe = r'''
import sys, json
import presidio_analyzer


class Boom:
    def __init__(self, *a, **k):
        raise OSError("injected: no spaCy model")


presidio_analyzer.AnalyzerEngine = Boom

from starlette.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    body = client.get("/api/v1/health").json()
print(json.dumps(body["pii_detector"]))
'''
    env = {
        **os.environ,
        "APP_SECRET_KEY": uuid.uuid4().hex,
        "LIBRERUN_PII_ALLOW_DEGRADED": "false",
        "LOG_STDERR_ENABLED": "false",
    }
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out.returncode == 0, f"the chassis did not boot:\n{out.stderr[-4000:]}"
    detector = json.loads(out.stdout.strip().splitlines()[-1])
    assert detector["state"] == "unavailable"
    assert detector["coverage"] == "regex_only"
    assert detector["error"] == "OSError"


# --------------------------------------------------------------------------
# Intake, the preview and the upload: 503
# --------------------------------------------------------------------------


class _PiiAgent(AgentProtocol):
    """One free-text field, marked ``x-pii`` — the shape intake redacts."""

    agent_id = "pii-fixture-agent"
    display_name = "pii fixture"
    description = "pii fixture"

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "required": ["summary"],
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "minLength": 5},
                "logs_a": {"type": "string", "x-pii": True},
            },
        }

    async def analyze(self, inp, on_progress):  # pragma: no cover
        return AnalysisResult(display={}, structured={})

    async def investigate(self, inp, on_progress):  # pragma: no cover
        return InvestigationResult(status="complete")


def test_intake_refuses_before_it_looks_at_a_single_field():
    """The gate is unconditional, and that is the point.

    A payload whose ``x-pii`` fields are empty — or a schema marking
    none — would otherwise create a run against a detector that cannot
    redact, and the next thing that run does is hand agent output back
    across the boundary to be persisted. The promise is about the run.
    """
    from app.services.intake import redact_pii_fields

    schema = _PiiAgent().input_schema()
    with constructor_raises():
        with pytest.raises(pii_service.PiiDetectorUnavailable) as excinfo:
            redact_pii_fields(schema, {"summary": "nothing marked x-pii here"})
    assert excinfo.value.stage == "intake"


@contextmanager
def _real_app(db, *, user_id, tenant_id, role="customer"):
    """The REAL ``app.main.app``, with the session and the principal
    overridden. Not a bare router harness: the 503 these tests assert is
    produced by an exception handler registered on the real app, so a
    harness that re-registers the routes would be testing a wiring
    nobody ships.
    """
    from app.main import app as chassis_app

    principal = SimpleNamespace(
        id=user_id,
        email="probe@example.com",
        role=role,
        tenant_id=tenant_id,
        is_platform_admin=(role == "admin"),
    )

    async def _db_dep():
        yield db

    async def _user_dep(request: Request):
        request.state.tenant_id = tenant_id
        request.state.current_user = principal
        request.state.session_id = uuid.uuid4()
        return principal

    chassis_app.dependency_overrides[get_db] = _db_dep
    chassis_app.dependency_overrides[get_current_user] = _user_dep
    try:
        yield AsyncClient(
            transport=ASGITransport(app=chassis_app), base_url="http://testserver"
        )
    finally:
        chassis_app.dependency_overrides.pop(get_db, None)
        chassis_app.dependency_overrides.pop(get_current_user, None)


@pytest_asyncio.fixture
async def db():
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import settings

    engine = create_async_engine(settings.DATABASE_URL.get_secret_value())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        reason = f"no database at DATABASE_URL ({type(exc).__name__}: {exc})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        # ``expire_on_commit=False`` is the app's own sessionmaker
        # setting (app/database.py). Without it, ``create_run``'s
        # ``db.commit()`` expires the Run and reading ``run.id`` one line
        # later fires a lazy refresh — a fixture that does not match the
        # deployment turns into a failure in the code under test.
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def world(db):
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, slug) VALUES (:id, :n, :s)"),
        {"id": tenant_id, "n": "S4c", "s": f"s4c-{tenant_id.hex[:12]}"},
    )
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, email, auth_provider, role)"
            " VALUES (:id, :t, :e, 'google', 'customer')"
        ),
        {"id": user_id, "t": tenant_id, "e": f"{user_id.hex[:10]}@example.com"},
    )
    await db.flush()
    return {"tenant_id": tenant_id, "user_id": user_id}


@pytest.fixture
def pii_agent():
    from app.agents import registry

    registry._clear_registry_for_tests()
    registry.register(_PiiAgent())
    yield _PiiAgent.agent_id
    registry._clear_registry_for_tests()


async def _run_rows(db, tenant_id) -> int:
    result = await db.execute(
        text("SELECT count(*) FROM runs WHERE tenant_id = :t"), {"t": tenant_id}
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_intake_answers_503_and_no_run_is_created(world, db, pii_agent):
    payload = {"summary": "handshake failure", "logs_a": FIXTURE_TEXT}
    before = await _run_rows(db, world["tenant_id"])

    with constructor_raises():
        with _real_app(db, user_id=world["user_id"], tenant_id=world["tenant_id"]) as c:
            r = await c.post(f"/api/v1/runs?agent_id={pii_agent}", json=payload)

    assert r.status_code == 503, r.text
    body = r.json()
    assert body["code"] == "pii_detector_unavailable"
    assert body["pii_detector"]["state"] == "unavailable"
    assert FIXTURE_NAME not in r.text, "the refusal must not echo what it refused"
    assert await _run_rows(db, world["tenant_id"]) == before, (
        "a refused intake created a run anyway"
    )


@pytest.mark.asyncio
async def test_intake_succeeds_when_the_detector_is_ready(world, db, pii_agent):
    """The control. Without it, a 503 for the wrong reason reads as a pass."""
    payload = {"summary": "handshake failure", "logs_a": FIXTURE_TEXT}
    before = await _run_rows(db, world["tenant_id"])

    pii_service.warm_detector(force=True)
    with patch("app.services.agent_runner.start_run"):
        with _real_app(db, user_id=world["user_id"], tenant_id=world["tenant_id"]) as c:
            r = await c.post(f"/api/v1/runs?agent_id={pii_agent}", json=payload)

    assert r.status_code == 202, r.text
    assert await _run_rows(db, world["tenant_id"]) == before + 1


@pytest.mark.asyncio
async def test_the_preview_answers_503(world, db):
    with constructor_raises():
        with _real_app(db, user_id=world["user_id"], tenant_id=world["tenant_id"]) as c:
            r = await c.post(
                "/api/v1/files/redact-preview",
                files={"file": ("auth.log", FIXTURE_TEXT, "text/plain")},
                data={"file_type": "log"},
            )
    assert r.status_code == 503, r.text
    assert r.json()["code"] == "pii_detector_unavailable"
    assert FIXTURE_NAME not in r.text


@pytest.mark.asyncio
async def test_the_upload_answers_503_and_writes_nothing(world, db, tmp_path):
    run_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO runs (id, tenant_id, user_id, run_number, status,"
            " problem_statement) VALUES (:id, :t, :u, 'RUN-9001', 'submitted', 'x')"
        ),
        {"id": run_id, "t": world["tenant_id"], "u": world["user_id"]},
    )
    await db.flush()

    from app.routers import files as files_router

    with patch.object(files_router.settings, "FILE_STORAGE_PATH", str(tmp_path)):
        with constructor_raises():
            with _real_app(
                db, user_id=world["user_id"], tenant_id=world["tenant_id"]
            ) as c:
                r = await c.post(
                    f"/api/v1/runs/{run_id}/files",
                    files={"file": ("auth.log", FIXTURE_TEXT, "text/plain")},
                    data={"vendor_side": "a", "file_type": "log"},
                )

    assert r.status_code == 503, r.text
    assert r.json()["code"] == "pii_detector_unavailable"
    assert not list(tmp_path.rglob("*")), "the refused upload still wrote a file"
    rows = await db.execute(
        text("SELECT count(*) FROM run_files WHERE run_id = :r"), {"r": run_id}
    )
    assert int(rows.scalar_one()) == 0


# --------------------------------------------------------------------------
# The run boundary
# --------------------------------------------------------------------------


def test_the_run_boundary_refuses_agent_output():
    """The same refusal shape as ``pii_in_output`` today, so
    ``agent_runner`` ends the run ``error`` with nothing persisted."""
    with analyze_raises():
        with pytest.raises(run_boundary.PiiRefused) as excinfo:
            run_boundary.walk_value(
                {"summary": FIXTURE_TEXT},
                argument="output",
                reason=run_boundary.REASON_OUTPUT,
            )
    exc = excinfo.value
    assert exc.reason == run_boundary.REASON_OUTPUT
    assert exc.finding.kind == run_boundary.DETECTOR_KIND
    assert exc.finding.pii_type == run_boundary.DETECTOR_PII_TYPE
    assert FIXTURE_NAME not in str(exc)


def test_the_run_boundary_refuses_free_text_and_audit_writes():
    with constructor_raises():
        with pytest.raises(run_boundary.PiiRefused) as out:
            run_boundary.redact_text(FIXTURE_TEXT)
        assert out.value.reason == run_boundary.REASON_OUTPUT

        with pytest.raises(run_boundary.PiiRefused) as audit:
            run_boundary.walk_value(
                {"detail": FIXTURE_TEXT},
                argument="detail",
                reason=run_boundary.REASON_AUDIT,
            )
        assert audit.value.reason == run_boundary.REASON_AUDIT


def test_the_container_adapter_drops_a_progress_event_it_could_not_walk():
    """A progress event has no reply channel: the adapter drops it with
    the position named and the run continues — the rule the flagged
    ``step_id`` already followed."""
    import asyncio

    from app.agents.container import ContainerAgent

    agent = ContainerAgent.__new__(ContainerAgent)
    agent.agent_id = "probe"
    seen = []

    async def on_progress(p):  # pragma: no cover - must not be reached
        seen.append(p)

    async def drive():
        with constructor_raises():
            return await agent._handle_event(
                "progress",
                {"step_id": "step-1", "status": "running", "detail": FIXTURE_TEXT},
                on_progress,
            )

    assert asyncio.run(drive()) is None
    assert seen == [], "a detail the walk could not complete was forwarded anyway"


# --------------------------------------------------------------------------
# The MCP redact tool
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_mcp_redact_tool_answers_a_detector_error():
    import redis.asyncio as aioredis

    from app.config import settings
    from app.main import app as chassis_app

    token = f"s4c-{uuid.uuid4().hex}"
    run = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(uuid.uuid4()),
        "agent_id": "s4c-probe",
        "grants": ["pii"],
    }
    try:
        async with aioredis.from_url(
            settings.REDIS_URL.get_secret_value(), decode_responses=True
        ) as r:
            await r.set(f"run_token:{token}", json.dumps(run), ex=300)
    except Exception as exc:  # noqa: BLE001
        reason = f"no redis at REDIS_URL ({type(exc).__name__}: {exc})"
        if REQUIRE_DB:
            pytest.fail("LIBRERUN_REQUIRE_DB is set, so this may not skip: " + reason)
        pytest.skip(reason)

    client = TestClient(chassis_app)
    body = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "redact", "arguments": {"text": FIXTURE_TEXT}},
    }
    with constructor_raises():
        response = client.post(
            "/api/v1/mcp", json=body, headers={"Authorization": f"Bearer {token}"}
        )

    error = response.json()["error"]
    assert error["code"] == -32004, response.text
    assert "pii_detector_unavailable" in error["message"]
    assert FIXTURE_NAME not in response.text


def test_the_sdk_surfaces_the_detector_error_as_pii_unavailable():
    """The code the chassis answers with is the one the SDK maps, and a
    bare ``CapabilityError`` would let an agent retry forever."""
    sdk_src = BACKEND_DIR.parent / "sdk" / "python" / "librerun-agent" / "src"
    if str(sdk_src) not in sys.path:  # pragma: no cover - conftest adds it
        sys.path.insert(0, str(sdk_src))
    from librerun_agent import PiiRefused as SdkPiiRefused
    from librerun_agent import PiiUnavailable
    from librerun_agent._mcp import MCPClient

    client = MCPClient("http://chassis.invalid/mcp", "token")
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32004, "message": "pii_detector_unavailable: …"},
        }
    ).encode()

    class _Response:
        def read(self):
            return payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", return_value=_Response()):
        with pytest.raises(PiiUnavailable) as excinfo:
            client._call_sync("redact", {"text": "x"})
    assert excinfo.value.code == -32004
    assert not isinstance(excinfo.value, SdkPiiRefused), (
        "a detector outage is not a flagged value; an agent must tell them apart"
    )


# --------------------------------------------------------------------------
# Telemetry: stripped to identity, never exported unwalked
# --------------------------------------------------------------------------


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="agent.probe",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg={"event": message},
        args=(),
        exc_info=None,
    )


def test_a_log_record_is_dropped_rather_than_walked_half_way():
    record = _record(FIXTURE_TEXT)
    with constructor_raises():
        with pytest.raises(walkers.RecordRefused) as excinfo:
            walkers.walk_log_record(record)
    assert excinfo.value.kind == "detector"
    assert FIXTURE_NAME not in str(excinfo.value)


def test_a_marked_log_field_becomes_the_failure_placeholder():
    """``logging_pii`` is the fourth export point and it already had the
    right answer: a marked user-content field whose redaction raises is
    replaced by ``[REDACTION_FAILED]`` rather than emitted. Pinned here
    because S4c makes that path reachable for the first time — before it,
    ``redact`` could not raise at all, so the branch existed and nothing
    could enter it.
    """
    from app.logging_pii import (
        REDACTION_FAILED,
        redact_user_content_processor,
        user_content,
    )

    with constructor_raises():
        event = redact_user_content_processor(
            None, "info", {"note": user_content(FIXTURE_TEXT)}
        )
    assert event["note"] == REDACTION_FAILED
    assert FIXTURE_NAME not in json.dumps(event)


def _one_span_request(name: str, attribute: str | None):
    """One span. ``attribute=None`` leaves the span NAME as the only
    content position in it — without that, every other string in the
    span flags it too (an unavailable detector refuses every string,
    whatever it says), and a test meaning to isolate the name would pass
    on its neighbour's flag.
    """
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    request = trace_service_pb2.ExportTraceServiceRequest()
    resource_spans = request.resource_spans.add()
    scope_spans = resource_spans.scope_spans.add()
    span = scope_spans.spans.add()
    span.name = name
    span.trace_id = b"\x01" * 16
    span.span_id = b"\x02" * 8
    if attribute is not None:
        kv = span.attributes.add()
        kv.key = "agent.note"
        kv.value.string_value = attribute
    return request, span


def test_an_exported_span_is_stripped_to_its_identity():
    request, _ = _one_span_request("phase", FIXTURE_TEXT)
    with constructor_raises():
        report = otlp_walk.walk_trace_request(request)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert report.spans_stripped == 1
    assert report.detector_unavailable >= 1
    assert span.name == otlp_walk.STRIPPED_SPAN_NAME
    assert span.trace_id == b"\x01" * 16, "identity survives the strip"
    exported = {kv.key: kv.value.string_value for kv in span.attributes}
    assert FIXTURE_NAME not in json.dumps(exported)
    assert exported.get(otlp_walk.REDACTED_REASON_ATTRIBUTE) == otlp_walk.DETECTOR_FLAG


def test_an_exported_span_name_the_walk_could_not_read_strips_the_span():
    """The span NAME is content too. Its flag used to be discarded
    because content could never raise one.

    The span carries NOTHING else — no attributes, no events, no links —
    so only the name can flag it. With an attribute beside it the test
    passed even with the name's flag thrown away, which is what the
    mutation run found.
    """
    request, _ = _one_span_request(FIXTURE_TEXT, None)
    with constructor_raises():
        report = otlp_walk.walk_trace_request(request)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert report.spans_stripped == 1
    assert span.name == otlp_walk.STRIPPED_SPAN_NAME


def _one_log_request(body: str | None = None, *, severity_text: str | None = None):
    """One log record with exactly the content positions asked for, for
    the reason ``_one_span_request`` spells out: an unavailable detector
    flags EVERY string, so a record carrying two of them cannot say
    which one did it."""
    from opentelemetry.proto.collector.logs.v1 import logs_service_pb2

    request = logs_service_pb2.ExportLogsServiceRequest()
    scope_logs = request.resource_logs.add().scope_logs.add()
    scope_logs.scope.name = "agent.probe"
    record = scope_logs.log_records.add()
    if body is not None:
        record.body.string_value = body
    if severity_text is not None:
        record.severity_text = severity_text
    return request


def test_an_exported_log_record_is_dropped():
    request = _one_log_request(FIXTURE_TEXT)
    with constructor_raises():
        report = otlp_walk.walk_logs_request(request)
    kept = request.resource_logs[0].scope_logs[0].log_records
    assert report.records_dropped == 1
    assert len(kept) == 0
    assert FIXTURE_NAME not in str(request)


def test_an_exported_records_severity_text_is_content_too():
    """``severity_text`` is CONTENT in the classification table, and its
    flag was discarded for the same reason the span name's was. The
    record carries nothing else, so only it can flag the record."""
    request = _one_log_request(severity_text=FIXTURE_NAME)
    with constructor_raises():
        report = otlp_walk.walk_logs_request(request)
    assert report.records_dropped == 1
    assert len(request.resource_logs[0].scope_logs[0].log_records) == 0


def test_an_exported_span_events_name_is_content_too():
    """And so is ``Span.Event.name`` — the third position whose flag the
    walk used to throw away."""
    # The span NAME is blank, so it is not a content position at all
    # (``_apply`` skips an empty value) and the event's name is the only
    # string the walk meets.
    request, span = _one_span_request("", None)
    span.events.add().name = FIXTURE_NAME
    with constructor_raises():
        report = otlp_walk.walk_trace_request(request)
    walked = request.resource_spans[0].scope_spans[0].spans[0]
    assert report.spans_stripped == 1
    assert walked.name == otlp_walk.STRIPPED_SPAN_NAME
    assert len(walked.events) == 0, "a stripped span keeps no events"


# --------------------------------------------------------------------------
# The opt-out: regex-only, with the stamp and the audit row
# --------------------------------------------------------------------------


def test_the_opt_out_restores_the_regex_only_pass_through(monkeypatch):
    with constructor_raises(), opt_out(monkeypatch):
        redacted, _ = pii_service.redact(f"{FIXTURE_TEXT} from 10.0.0.9")
    # Exactly today's behaviour: the regex stages still run, stage 3
    # does not — which is why the name survives and the IP does not.
    assert FIXTURE_NAME in redacted
    assert "10.0.0.9" not in redacted
    assert pii_service.detector_counters()["degraded_calls"] >= 1


def test_the_opt_out_stamps_the_span_it_touched(monkeypatch):
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)

    with constructor_raises(), opt_out(monkeypatch):
        with tracer.start_as_current_span("phase"):
            pii_service.redact(FIXTURE_TEXT)

    span = exporter.get_finished_spans()[0]
    assert span.attributes.get(pii_service.DEGRADED_ATTRIBUTE) is True


def test_the_opt_out_stamps_the_log_record_it_touched(monkeypatch):
    record = _record(FIXTURE_TEXT)
    with constructor_raises(), opt_out(monkeypatch):
        walked = walkers.walk_log_record(record)
    assert getattr(walked, pii_service.DEGRADED_EVENT_FIELD) is True
    assert walked.msg[pii_service.DEGRADED_EVENT_FIELD] is True
    # And the record is kept, regex-only, rather than dropped.
    assert FIXTURE_NAME in walked.msg["event"]


def test_the_opt_out_stamps_the_exported_span_and_keeps_it(monkeypatch):
    request, _ = _one_span_request("phase", FIXTURE_TEXT)
    with constructor_raises(), opt_out(monkeypatch):
        report = otlp_walk.walk_trace_request(request)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert report.spans_stripped == 0
    assert report.degraded == 1
    assert span.name == "phase"
    stamped = {kv.key: kv.value.bool_value for kv in span.attributes}
    assert stamped.get(pii_service.DEGRADED_ATTRIBUTE) is True


def test_the_opt_out_announces_itself_at_startup(monkeypatch, caplog):
    with constructor_raises(), opt_out(monkeypatch):
        with caplog.at_level(logging.WARNING):
            status = pii_service.warm_detector()
    assert status.allow_degraded is True
    assert any(
        "pii_detector_degraded_allowed" in r.getMessage() for r in caplog.records
    ), "the opt-out must be visible on the platform plane at boot"


@pytest.mark.asyncio
async def test_the_opt_out_writes_the_audit_row_for_the_tenant_it_served(
    world, db, monkeypatch
):
    import structlog

    tenant_id = world["tenant_id"]
    structlog.contextvars.bind_contextvars(tenant_id=str(tenant_id))
    try:
        with constructor_raises(), opt_out(monkeypatch):
            pii_service.redact(FIXTURE_TEXT, stage="intake")
            assert pii_service.detector_counters()["audit_queue"] == 1
            with patch("app.database.async_session", _session_factory(db)):
                written = await pii_service.flush_degraded_audits()
    finally:
        structlog.contextvars.clear_contextvars()

    assert written == 1
    rows = await db.execute(
        text(
            "SELECT detail FROM activity_audit_log"
            " WHERE tenant_id = :t AND action_type = 'pii_detector_degraded'"
        ),
        {"t": tenant_id},
    )
    detail = rows.scalar_one()
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail["stage"] == "intake"
    assert detail["state"] == "unavailable"
    assert detail["coverage"] == "regex_only"
    assert FIXTURE_NAME not in json.dumps(detail)


def _session_factory(session):
    """``async_session()`` over the test's own transaction-bound session,
    so the row is visible to this test and rolled back after it.

    ``begin()`` becomes ``begin_nested()`` and nothing else changes: in
    production ``flush_degraded_audits`` opens a FRESH session and its
    ``async with db.begin()`` is the outermost transaction, while here
    the fixture already holds one for the tenant row the audit row
    references. A SAVEPOINT is the same write with the same commit
    semantics inside a transaction this test still rolls back.
    """
    from contextlib import asynccontextmanager

    class _Proxy:
        def __getattr__(self, name):
            return getattr(session, name)

        def begin(self):
            return session.begin_nested()

    @asynccontextmanager
    async def _factory():
        yield _Proxy()

    def _call():
        return _factory()

    return _call


# --------------------------------------------------------------------------
# One policy, both processes
# --------------------------------------------------------------------------


# The two services that run ``pii_service``: the backend imports it
# directly, and the gateway's image copies ``backend/app`` beside its own
# package (``services/gateway/Dockerfile``) so its outbound redaction is
# the same pipeline. Named rather than derived, because "which compose
# service imports which module" is not a thing compose.yaml knows.
_REDACTING_SERVICES = ("backend", "gateway")
_POLICY_VARIABLE = "LIBRERUN_PII_ALLOW_DEGRADED"


def test_the_opt_out_reaches_every_process_that_redacts():
    """One setting has to be one policy.

    ``compose.yaml`` delivers only the variables it names — there is no
    ``env_file`` handing either service the whole of ``.env`` — so a
    policy variable added to the settings model and not to both blocks
    is a switch that silently does nothing in container mode, which is
    the mode ``docs/platform/Install.md`` documents. Caught on the merge with K1,
    where this had already happened once to ``PII_PHONE_REGION``: the
    comment above it in ``compose.yaml`` records that a phone number the
    backend refused reached a provider through the gateway, because the
    region was passed to one service and not the other.

    Asserts the same default on both, too: two blocks that both name the
    variable but disagree about what happens when it is unset are the
    same bug wearing a hat.
    """
    import yaml

    compose = yaml.safe_load((BACKEND_DIR.parent / "compose.yaml").read_text())
    defaults = {}
    for service in _REDACTING_SERVICES:
        environment = compose["services"][service]["environment"]
        if not isinstance(environment, dict):
            environment = dict(e.split("=", 1) for e in environment)
        assert _POLICY_VARIABLE in environment, (
            f"compose does not pass {_POLICY_VARIABLE} to {service!r}, which "
            "runs pii_service — the opt-out would do nothing there while "
            "working in the other process, so the deployment would fail "
            "closed at one door and pass names at the other"
        )
        defaults[service] = environment[_POLICY_VARIABLE]

    assert len(set(defaults.values())) == 1, (
        f"the services disagree about {_POLICY_VARIABLE}: {defaults}"
    )
    assert ":-false}" in next(iter(defaults.values())), (
        f"{_POLICY_VARIABLE} must default to false — the platform fails "
        f"closed unless an operator opts out: {defaults}"
    )


def test_the_settings_model_defaults_the_opt_out_to_false():
    """The other half of the same claim: an operator who sets nothing
    anywhere gets the fail-closed platform, not the degraded one."""
    from app.config import Settings

    assert Settings().LIBRERUN_PII_ALLOW_DEGRADED is False


# --------------------------------------------------------------------------
# The smoke's own assertion
# --------------------------------------------------------------------------


def _smoke():
    import importlib.util

    path = BACKEND_DIR.parent / "scripts" / "librerun_smoke.py"
    spec = importlib.util.spec_from_file_location("librerun_smoke_s4c", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "health",
    [
        pytest.param({"status": "ok"}, id="no pii_detector block at all"),
        pytest.param(
            {"pii_detector": {"state": "unavailable", "coverage": "regex_only"}},
            id="the spaCy model never arrived",
        ),
        pytest.param(
            {"pii_detector": {"state": "failed", "coverage": "regex_only"}},
            id="a call raised",
        ),
    ],
)
def test_the_smoke_refuses_a_deployment_whose_detector_is_not_ready(health):
    """A 200 from /health says the process is up, not that stage 3 can
    run. Tested against fabricated bodies rather than only exercised
    against a live one, because a checker that accepts everything is the
    failure mode and a healthy deployment cannot reveal it."""
    smoke = _smoke()
    with pytest.raises(smoke.SmokeFailure) as excinfo:
        smoke.assert_detector_ready(health)
    assert "pii_detector" in str(excinfo.value)


def test_the_smoke_accepts_a_ready_detector():
    smoke = _smoke()
    smoke.assert_detector_ready(
        {"pii_detector": {"state": "ready", "coverage": "ner"}}
    )


# --------------------------------------------------------------------------
# The negative probe
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_negative_probe_without_the_policy_the_name_reaches_the_database(
    world, db, pii_agent
):
    """Remove the policy and watch the fixture's name land in a row.

    This is the test that makes the rest of the module mean something.
    ``LIBRERUN_PII_ALLOW_DEGRADED=true`` is not a mock of the missing
    check — it IS the check removed, spelled the way an operator spells
    it — and with it set the run is created and ``user_inputs`` in the
    ``runs`` table contains ``Marguerite Okonkwo`` verbatim, exactly as
    every run did before S4c.

    Paired deliberately with ``test_intake_answers_503_and_no_run_is_created``
    above: same payload, same detector fault, opposite outcome. If the
    fail-closed path were ever hollowed out — the gate deleted, the
    exception swallowed — that test would go green against a platform
    behaving the way THIS one describes, and only the pair can tell
    those two worlds apart.
    """
    import structlog

    payload = {"summary": "handshake failure", "logs_a": FIXTURE_TEXT}
    monkey = pytest.MonkeyPatch()
    structlog.contextvars.bind_contextvars(tenant_id=str(world["tenant_id"]))
    try:
        with constructor_raises(), opt_out(monkey):
            with patch("app.services.agent_runner.start_run"):
                with _real_app(
                    db, user_id=world["user_id"], tenant_id=world["tenant_id"]
                ) as c:
                    r = await c.post(
                        f"/api/v1/runs?agent_id={pii_agent}", json=payload
                    )
    finally:
        monkey.undo()
        structlog.contextvars.clear_contextvars()

    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]

    dump = await db.execute(
        text("SELECT user_inputs::text FROM runs WHERE id = :id"), {"id": run_id}
    )
    stored = dump.scalar_one()
    assert FIXTURE_NAME in stored, (
        "the probe did not reproduce the gap: with the policy removed the "
        "fixture's name is supposed to reach the database, and if it does "
        "not, the fail-closed tests above are not measuring what they claim"
    )
    assert FIXTURE_PLACE in stored
