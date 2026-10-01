"""Relay tests for the browser telemetry endpoint (LibreRun RUM v1).

The relay is the trust boundary, so the tests exercise it as one:
authentication precedes everything except the kill switch, identity is
underivable from the payload, per-record failures drop without killing
the batch, surface claims are authorized against registry + tenant runs,
and the quota guard is tested in BOTH directions (fires on violation,
stays quiet on a healthy request) per the house rule on gates.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

import app.config as cfg
from app.database import get_db
from app.middleware import get_current_user
from app.routers import ux_telemetry


TENANT_ID = uuid.uuid4()
USER_ID = uuid.uuid4()
RUN_ID = uuid.uuid4()
PAGE_ID = str(uuid.uuid4())
SESSION_ID = str(uuid.uuid4())


def _fake_user() -> SimpleNamespace:
    return SimpleNamespace(id=USER_ID, tenant_id=TENANT_ID)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)


class _FakeDb:
    """Answers the relay's single batched Run query."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = 0

    async def execute(self, stmt):
        self.queries += 1
        return _FakeResult(self.rows)


class _FakePipeline:
    def __init__(self, counters, ops_sink=None):
        self._counters = counters
        self.ops = []
        self._sink = ops_sink

    def _record(self, op):
        self.ops.append(op)
        if self._sink is not None:
            self._sink.append(op)

    def incrby(self, key, cost):
        self._record(("incrby", key, cost))
        return self

    def expire(self, key, ttl):
        self._record(("expire", key, ttl))
        return self

    async def execute(self):
        out = []
        for counter in self._counters:
            out.extend([counter, True])
        return out


class _FakeRedis:
    def __init__(self, counters=(1, 1, 1)):
        self.counters = list(counters)
        self.last_pipeline = None
        # Every op across every pipeline — the relay may charge in two
        # stages (bytes pre-parse, record top-up post-validation).
        self.all_ops = []

    def pipeline(self):
        self.last_pipeline = _FakePipeline(self.counters, self.all_ops)
        return self.last_pipeline


class _CaptureTelemetry:
    def __init__(self):
        self.calls = []

    def emit(self, ctx, records):
        self.calls.append((ctx, records))
        return len(records)


@pytest.fixture
def capture(monkeypatch):
    cap = _CaptureTelemetry()
    monkeypatch.setattr(ux_telemetry, "get_web_telemetry", lambda: cap)
    monkeypatch.setattr(
        ux_telemetry, "get_manifest", lambda aid: object() if aid == "vita-v1" else None
    )

    async def _fake_get_redis():
        return _FakeRedis()

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    return cap


def _app(db_rows=None, *, authenticated=True) -> FastAPI:
    app = FastAPI()
    app.include_router(ux_telemetry.router, prefix="/api/v1")
    fake_db = _FakeDb(db_rows)

    async def _db():
        yield fake_db

    app.dependency_overrides[get_db] = _db
    if authenticated:
        app.dependency_overrides[get_current_user] = _fake_user
    app.state.fake_db = fake_db
    return app


def _record(**overrides) -> dict:
    # The clock is read here, not at import: the relay judges a record
    # against its own clock when the request arrives, and a suite can
    # reach this module many minutes after collecting it.
    rec = {
        "type": "web_vital",
        "occurred_at_ms": ux_telemetry.now_ms(),
        "page_id": PAGE_ID,
        "route": "run.detail",
        "ui_owner": "platform",
        "name": "lcp",
        "value": 1234.5,
        "rating": "good",
        "metric_id": "v4-1712345678901-1234567890123",
    }
    rec.update(overrides)
    return rec


def _envelope(records, **overrides) -> dict:
    env = {"schema_version": 1, "session_id": SESSION_ID, "records": records}
    env.update(overrides)
    return env


def _post(client: TestClient, envelope: dict, headers=None):
    return client.post(
        "/api/v1/_o/e",
        content=json.dumps(envelope),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


# --- authentication and kill switch -------------------------------------


def test_unauthenticated_is_401(capture):
    client = TestClient(_app(authenticated=False))
    res = _post(client, _envelope([_record()]))
    assert res.status_code == 401
    assert capture.calls == []


def test_kill_switch_answers_410_before_auth(capture, monkeypatch):
    monkeypatch.setattr(cfg.settings, "UX_TELEMETRY_ENABLED", False)
    # No auth override and no Authorization header: 410 must still win,
    # proving the switch costs nothing and precedes authentication.
    client = TestClient(_app(authenticated=False))
    res = _post(client, _envelope([_record()]))
    assert res.status_code == 410
    assert capture.calls == []


# --- happy path and identity stamping ------------------------------------


def test_platform_record_accepted_and_stamped(capture):
    client = TestClient(_app())
    res = _post(client, _envelope([_record()]))
    assert res.status_code == 202
    assert res.json() == {"accepted": 1, "dropped": 0, "reasons": {}}
    ctx, records = capture.calls[0]
    assert ctx.tenant_id == str(TENANT_ID)
    assert ctx.session_id == SESSION_ID
    assert records[0].name == "lcp"
    # Pseudonym: 32 hex chars, keyed, and never the raw user id.
    assert len(ctx.user_pseudonym) == 32
    int(ctx.user_pseudonym, 16)
    assert str(USER_ID).replace("-", "") not in ctx.user_pseudonym


def test_pseudonym_is_deterministic_and_tenant_scoped():
    a = ux_telemetry.user_pseudonym(TENANT_ID, USER_ID)
    b = ux_telemetry.user_pseudonym(TENANT_ID, USER_ID)
    other = ux_telemetry.user_pseudonym(uuid.uuid4(), USER_ID)
    assert a == b
    assert a != other


def test_identity_cannot_be_claimed_in_envelope(capture):
    client = TestClient(_app())
    res = _post(client, _envelope([_record()], tenant_id="someone-else"))
    assert res.status_code == 400  # extra fields are rejected, not ignored


# --- per-record validation ------------------------------------------------


def test_bad_record_drops_alone(capture):
    client = TestClient(_app())
    bad = _record(sneaky="field")
    res = _post(client, _envelope([_record(), bad]))
    assert res.status_code == 202
    body = res.json()
    assert body["accepted"] == 1
    assert body["reasons"] == {"schema": 1}


def test_route_outside_the_closed_set_drops(capture):
    """Charset-valid free text must NOT ride the route field — the closed
    template set is enforced at the relay, not just in the frontend
    resolver an authenticated caller can bypass."""
    client = TestClient(_app())
    smuggle = _record(route="customer-acme-production-secret")
    res = _post(client, _envelope([smuggle, _record()]))
    body = res.json()
    assert body["accepted"] == 1
    assert body["reasons"] == {"schema": 1}


def test_bundle_module_requires_static_prefix(capture):
    client = TestClient(_app())
    base = {
        "type": "js_exception",
        "occurred_at_ms": ux_telemetry.now_ms(),
        "page_id": PAGE_ID,
        "route": "run.new",
        "ui_owner": "platform",
        "error_type": "TypeError",
        "mechanism": "window.error",
        "fingerprint": "deadbeef",
    }
    good = {**base, "bundle_module": "/_next/static/chunks/page.js"}
    smuggle = {**base, "bundle_module": "customer-secret"}
    res = _post(client, _envelope([good, smuggle]))
    body = res.json()
    assert body["accepted"] == 1
    assert body["reasons"] == {"schema": 1}


def test_metric_id_accepts_only_the_generated_web_vitals_shape(capture):
    """`browser.web_vital.id` is exported verbatim, so a bare identifier
    charset was a free-text channel for callers that skip the frontend.
    Only the library's `v{major}-{ts}-{random}` digits-only shape passes."""
    client = TestClient(_app())
    smuggle = _record(metric_id="customer-acme-production-secret")
    canary = _record(metric_id="LIBRERUN_SECRET_CANARY_9f31")
    res = _post(client, _envelope([smuggle, canary, _record()]))
    assert res.status_code == 202
    assert res.json()["accepted"] == 1
    assert res.json()["reasons"] == {"schema": 2}
    ((_, records),) = capture.calls
    assert [r.metric_id for r in records] == ["v4-1712345678901-1234567890123"]


def test_clock_skew_is_rejected(capture):
    """Both edges of the window, measured from the clock when the test
    runs. Measured from the clock at import, "ten minutes ahead" stopped
    being ahead of the relay once a suite reached this test eight minutes
    after collection (the window's future edge is two), and a slow full
    run failed here with nothing wrong in the relay."""
    client = TestClient(_app())
    now = ux_telemetry.now_ms()
    stale = _record(occurred_at_ms=now - 4 * 60 * 60 * 1000)
    future = _record(occurred_at_ms=now + 10 * 60 * 1000)
    res = _post(client, _envelope([stale, future, _record()]))
    body = res.json()
    assert body["accepted"] == 1
    assert body["reasons"] == {"skew": 2}


def test_malformed_json_is_400(capture):
    client = TestClient(_app())
    res = client.post(
        "/api/v1/_o/e",
        content=b"{not json",
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 400


def test_nonfinite_json_numbers_are_400(capture):
    client = TestClient(_app())
    raw = (
        '{"schema_version": 1, "session_id": "%s", "records": '
        '[{"type": "web_vital", "value": NaN}]}' % SESSION_ID
    )
    res = client.post(
        "/api/v1/_o/e",
        content=raw.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 400


def test_record_cap_is_enforced(capture):
    client = TestClient(_app())
    res = _post(client, _envelope([_record() for _ in range(51)]))
    assert res.status_code == 400


def test_body_cap_is_413(capture):
    client = TestClient(_app())
    res = client.post(
        "/api/v1/_o/e",
        content=b"x" * (ux_telemetry.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 413


# --- surface authorization -------------------------------------------------


def _agent_record(**overrides) -> dict:
    base = {"ui_owner": "agent", "agent_id": "vita-v1", "run_id": str(RUN_ID)}
    base.update(overrides)
    return _record(**base)


def test_unregistered_agent_claim_is_dropped(capture):
    client = TestClient(_app(db_rows=[]))
    res = _post(client, _envelope([_agent_record(agent_id="ghost-v9", run_id=None)]))
    body = res.json()
    assert body["accepted"] == 0
    assert body["reasons"] == {"surface": 1}


def test_foreign_or_deleted_run_claim_is_dropped(capture):
    # The tenant-scoped query returns nothing for this run id.
    client = TestClient(_app(db_rows=[]))
    res = _post(client, _envelope([_agent_record()]))
    assert res.json()["reasons"] == {"surface": 1}


def test_run_of_other_agent_is_dropped(capture):
    row = SimpleNamespace(id=RUN_ID, agent_id="other-agent")
    client = TestClient(_app(db_rows=[row]))
    res = _post(client, _envelope([_agent_record()]))
    assert res.json()["reasons"] == {"surface": 1}


def test_owned_run_of_named_agent_is_accepted(capture):
    row = SimpleNamespace(id=RUN_ID, agent_id="vita-v1")
    client = TestClient(_app(db_rows=[row]))
    res = _post(client, _envelope([_agent_record()]))
    assert res.json() == {"accepted": 1, "dropped": 0, "reasons": {}}
    _, records = capture.calls[0]
    assert records[0].agent_id == "vita-v1"
    assert records[0].run_id == RUN_ID


def test_agent_surface_without_run_is_registry_checked_only(capture):
    client = TestClient(_app(db_rows=[]))
    res = _post(client, _envelope([_agent_record(run_id=None)]))
    assert res.json()["accepted"] == 1
    # No run ids claimed → the Run query must not even run.
    assert client.app.state.fake_db.queries == 0


# --- quotas: fire on violation AND stay quiet on healthy -------------------


def test_quota_429_with_retry_after(capture, monkeypatch):
    async def _fake_get_redis():
        return _FakeRedis(counters=(10_000, 1, 1))

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    client = TestClient(_app())
    res = _post(client, _envelope([_record()]))
    assert res.status_code == 429
    assert 0 < int(res.headers["Retry-After"]) <= 60
    assert capture.calls == []


def test_quota_quiet_on_healthy_traffic(capture, monkeypatch):
    seen = {}

    async def _fake_get_redis():
        redis = _FakeRedis(counters=(5, 5, 5))
        seen["redis"] = redis
        return redis

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    client = TestClient(_app())
    res = _post(client, _envelope([_record()]))
    assert res.status_code == 202
    # The guard really looked: three scopes, incrby+expire each.
    assert len(seen["redis"].last_pipeline.ops) == 6


def test_quota_fails_open_when_redis_hangs(capture, monkeypatch):
    """A Redis that accepts connections but never answers raises nothing —
    without the server-side deadline the fail-open except is unreachable
    and relay requests would pile up holding worker/DB resources."""
    import asyncio

    class _HangingPipeline:
        def incrby(self, *a):
            return self

        def expire(self, *a):
            return self

        async def execute(self):
            await asyncio.Event().wait()  # never set — hangs forever

    class _HangingRedis:
        def pipeline(self):
            return _HangingPipeline()

    async def _fake_get_redis():
        return _HangingRedis()

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    monkeypatch.setattr(ux_telemetry, "_QUOTA_TIMEOUT_SECONDS", 0.05)
    client = TestClient(_app())
    res = _post(client, _envelope([_record()]))
    assert res.status_code == 202


def test_quota_fails_open_when_redis_is_down(capture, monkeypatch):
    async def _broken_get_redis():
        raise ConnectionError("redis down")

    monkeypatch.setattr("app.redis.get_redis", _broken_get_redis)
    client = TestClient(_app())
    res = _post(client, _envelope([_record()]))
    assert res.status_code == 202


def test_quota_cost_is_records_and_bytes_weighted(capture, monkeypatch):
    """Charged in two stages (bytes pre-parse, record top-up), the TOTAL
    per bucket must still equal max(records, KiB)."""
    seen = {}

    async def _fake_get_redis():
        redis = seen.setdefault("redis", _FakeRedis())
        return redis

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    client = TestClient(_app())
    records = [_record() for _ in range(3)]
    _post(client, _envelope(records))
    per_key: dict = {}
    for op, key, cost in (o for o in seen["redis"].all_ops if o[0] == "incrby"):
        per_key[key] = per_key.get(key, 0) + cost
    assert len(per_key) == 3  # user / tenant / global buckets
    assert all(total == 3 for total in per_key.values())  # max(3 records, 1 KiB)


def test_malformed_requests_still_charge_quota(capture, monkeypatch):
    """A 400 must not be free: byte cost is charged BEFORE parsing, or an
    authenticated caller could loop malformed 128KiB bodies past the
    relay's only rate-based control."""
    seen = {}

    async def _fake_get_redis():
        redis = seen.setdefault("redis", _FakeRedis())
        return redis

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    client = TestClient(_app())
    res = client.post(
        "/api/v1/_o/e",
        content=b"{not json" + b"x" * 4096,
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 400
    charged = [op for op in seen["redis"].all_ops if op[0] == "incrby"]
    assert len(charged) == 3  # one per bucket
    assert all(cost >= 5 for _, _, cost in charged)  # ~5 KiB of bytes


def test_quota_429_precedes_parsing(capture, monkeypatch):
    async def _fake_get_redis():
        return _FakeRedis(counters=(10_000, 1, 1))

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    client = TestClient(_app())
    res = client.post(
        "/api/v1/_o/e",
        content=b"{definitely not json",
        headers={"Content-Type": "application/json"},
    )
    # Over-quota wins over malformed: the byte charge runs first.
    assert res.status_code == 429


def test_declared_overlimit_body_is_charged_at_the_cap(capture, monkeypatch):
    """A 413 must not be free either — declared path: Content-Length says
    too big, nothing is read, but the attempt is still priced at the cap
    so honesty about the size is never the cheap way to spam the relay."""
    seen = {}

    async def _fake_get_redis():
        return seen.setdefault("redis", _FakeRedis())

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    client = TestClient(_app())
    res = client.post(
        "/api/v1/_o/e",
        content=b"x" * (ux_telemetry.MAX_BODY_BYTES + 1),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 413
    charged = [op for op in seen["redis"].all_ops if op[0] == "incrby"]
    assert len(charged) == 3  # one per bucket
    assert all(
        cost == ux_telemetry.MAX_BODY_BYTES // 1024 for _, _, cost in charged
    )


def test_streamed_overlimit_body_is_charged_at_the_cap(capture, monkeypatch):
    """The review's attack shape: omit Content-Length (chunked transfer)
    so the precheck cannot fire, ship MAX+1, and the server reads and
    buffers the whole cap before it can 413. That work is charged."""
    seen = {}

    async def _fake_get_redis():
        return seen.setdefault("redis", _FakeRedis())

    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    client = TestClient(_app())

    def chunks():
        yield b"x" * ux_telemetry.MAX_BODY_BYTES
        yield b"x"

    res = client.post(
        "/api/v1/_o/e",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 413
    charged = [op for op in seen["redis"].all_ops if op[0] == "incrby"]
    assert len(charged) == 3
    assert all(
        cost == ux_telemetry.MAX_BODY_BYTES // 1024 for _, _, cost in charged
    )


def test_overlimit_loop_terminates_in_429(capture, monkeypatch):
    """The full loop from the finding: with the cap-cost charge, repeated
    over-limit bodies consume budget and hit 429 instead of looping free
    413s past the quota forever."""

    class _AccumulatingRedis:
        def __init__(self):
            self.buckets: dict = {}

        def pipeline(self):
            redis = self

            class _P:
                def __init__(self):
                    self.ops = []

                def incrby(self, key, cost):
                    self.ops.append((key, cost))
                    return self

                def expire(self, key, ttl):
                    return self

                async def execute(self):
                    out = []
                    for key, cost in self.ops:
                        redis.buckets[key] = redis.buckets.get(key, 0) + cost
                        out.extend([redis.buckets[key], True])
                    return out

            return _P()

    async def _fake_get_redis():
        return seen.setdefault("redis", _AccumulatingRedis())

    seen: dict = {}
    monkeypatch.setattr("app.redis.get_redis", _fake_get_redis)
    # Freeze the clock so both posts land in the same quota window.
    monkeypatch.setattr(
        ux_telemetry,
        "time",
        SimpleNamespace(time=lambda: 1_000_000.0, time_ns=lambda: 10**15),
    )
    # Two cap-cost charges (128 each) must overflow the user bucket.
    monkeypatch.setattr(cfg.settings, "UX_TELEMETRY_USER_UNITS_PER_MINUTE", 200)
    client = TestClient(_app())
    oversized = b"x" * (ux_telemetry.MAX_BODY_BYTES + 1)
    headers = {"Content-Type": "application/json"}
    first = client.post("/api/v1/_o/e", content=oversized, headers=headers)
    second = client.post("/api/v1/_o/e", content=oversized, headers=headers)
    assert first.status_code == 413  # charged 128 of 200
    assert second.status_code == 429  # 256 > 200 — quota supersedes 413
    assert second.headers["Retry-After"]
