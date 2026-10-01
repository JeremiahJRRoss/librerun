# NOTE: configure_logging() must run before any ``from app.X import Y`` below
# so downstream modules' ``logging.getLogger(__name__)`` handles inherit the
# JSON file + stderr handlers installed here. See backend/app/logging_config.py.
from app.logging_config import configure_logging, install_process_capture

configure_logging()
# Blueprint S4: with the queue-only pipeline on, replace sys.stdout /
# sys.stderr with context-capturing writers, route descriptors 1 and 2
# through the walking queue, and carry an invocation's context into the
# threads and executor work items it starts. A no-op when LOG_QUEUE_ONLY
# is false (the test suite).
install_process_capture()

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import async_session, get_db
from app.version import __license__, __version__
from app.redis import close_redis, get_redis
from app.services import app_settings_service
from app.routers import auth as auth_router
from app.routers import runs as runs_router
from app.routers import files as files_router
from app.routers import feedback as feedback_router
from app.routers import admin as admin_router
from app.routers import reports as reports_router
from app.routers import agents as agents_router
from app.routers import mcp as mcp_router
from app.routers import ux_telemetry as ux_telemetry_router
from app.routers import otlp_relay as otlp_relay_router

logger = structlog.get_logger(__name__)

# How long to wait between attempts to persist this boot's manifests.
# Short enough that a database coming up a minute late does not leave a
# removed agent authorized for long; long enough not to hammer one that
# is down.
RECONCILE_RETRY_SECONDS = 5.0


async def _reconcile_until_persisted() -> None:
    """Make THIS boot's discovery the rows the gateway reads.

    The gateway is a separate process and resolves every agent from
    `agent_manifests`, so until this succeeds it is working from the
    previous boot's rows: an agent uninstalled, or stripped of its `llm`
    grant, keeps authorizing model calls, and a run token left in Redis
    by the restart carries that authority to its TTL.

    This used to be one attempt and a warning — a warning that said "the
    gateway sees the previous boot's rows until this succeeds" while
    nothing existed that could ever make it succeed. The availability
    argument behind it was right and still holds: a database that is not
    up yet must not stop the API from booting. Giving up was the part
    that was wrong.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            from app.database import async_session
            from app.services.agent_snapshot_service import (
                reconcile_registered_agents,
            )

            async with async_session() as session:
                await reconcile_registered_agents(session)
                await session.commit()
        # No `except asyncio.CancelledError: raise` here. I wrote one,
        # then injected its removal and no test changed: `CancelledError`
        # derives from `BaseException`, so the clause below never catches
        # it and the re-raise had nothing to re-raise.
        #
        # I then wrote that the cancellation test would catch someone
        # widening this to `except BaseException` — and injected THAT,
        # and it passed too. Measured: asyncio keeps a pending cancel
        # and re-delivers it at the next `await`, so the sleep below
        # raises even if the clause swallowed the first one. Shutdown is
        # safe for a reason that has nothing to do with this line, which
        # is why the line is gone rather than kept as reassurance.
        except Exception as e:
            logger.warning(
                "agent_snapshot_reconcile_failed",
                attempt=attempt,
                error=str(e),
                error_type=type(e).__name__,
                hint="the LLM gateway resolves agents from agent_manifests; "
                "until this succeeds it sees the previous boot's rows. "
                "Retrying.",
            )
            await asyncio.sleep(RECONCILE_RETRY_SECONDS)
            continue
        if attempt > 1:
            logger.info("agent_snapshot_reconciled", attempt=attempt)
        return


async def _reconcile_orphans_until_done(boot_started_at: datetime) -> None:
    """Mark the runs the previous process left mid-phase (blueprint S7,
    gap H16) — beside the manifest reconcile above, and in its shape:
    retried until the database answers, because a database that is not
    up yet must not stop the API from booting, and giving up would leave
    a run that says it is running forever, which is the defect.

    ``boot_started_at`` is captured at the top of the lifespan, before
    discovery and the detector warm-up, so every run this process goes
    on to drive is written after it and is spared however late this
    succeeds (``agent_runner.reconcile_orphaned_runs`` explains the
    predicate).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            from app.database import async_session
            from app.services import agent_runner

            async with async_session() as session:
                reconciled = await agent_runner.reconcile_orphaned_runs(
                    session, boot_started_at=boot_started_at
                )
                await session.commit()
        except Exception as e:
            logger.warning(
                "orphaned_runs_reconcile_failed",
                attempt=attempt,
                error=str(e),
                error_type=type(e).__name__,
                hint="a run the previous process left mid-phase still says "
                "it is running until this succeeds. Retrying.",
            )
            await asyncio.sleep(RECONCILE_RETRY_SECONDS)
            continue
        logger.info(
            "orphaned_runs_reconciled",
            attempt=attempt,
            count=len(reconciled),
            run_ids=[r["run_id"] for r in reconciled],
        )
        return


async def _load_cors_origins() -> list[str]:
    """Load effective ``cors_origins`` from the settings service (DB override
    or ``.env`` default). Falls back to the ``.env`` list if the DB is
    unreachable at startup so the app can still boot.

    Runs under ``asyncio.run`` at import time — a THROWAWAY event loop that
    is closed before uvicorn starts its own. Any pooled asyncpg connection
    created here stays bound to that dead loop; if it leaks into the
    engine's pool, the first request to check it out fails with "got Future
    attached to a different loop" and the connection is then permanently
    poisoned ("cannot perform operation: another operation is in
    progress"). Dispose the engine before the bootstrap loop closes so
    serving starts with an empty pool and fresh connections are created on
    uvicorn's loop.
    """
    from app.database import engine

    try:
        async with async_session() as db:
            return await app_settings_service.get_setting(db, "cors_origins")
    except Exception:
        return settings.cors_origins_list
    finally:
        try:
            await engine.dispose()
        except Exception:
            pass


def _export_state_dir() -> None:
    """``LIBRERUN_STATE_DIR`` from ``.env`` into the process environment
    (K9-03, C12), before discovery.

    An agent reads the state directory from ``os.environ`` and never from
    ``app.config``, which an in-process agent is barred from, so a value
    set in ``.env`` alone reached the settings model and the deployment
    view but not the agent. A value the environment already carries wins:
    compose pins one, and an operator's export is theirs.
    """
    from app import config as _config

    value = (_config.settings.LIBRERUN_STATE_DIR or "").strip()
    if value and not os.environ.get("LIBRERUN_STATE_DIR"):
        os.environ["LIBRERUN_STATE_DIR"] = value


@asynccontextmanager
async def lifespan(app: FastAPI):
    # OTEL init runs at module scope (bottom of this file), not here: the
    # first ASGI call — the lifespan event itself — builds and caches the
    # middleware stack, and FastAPIInstrumentor works by patching
    # ``app.build_middleware_stack``, so a lifespan-time init would patch
    # too late and request spans would never be created. Agent discovery
    # still runs after the global TracerProvider is installed (import
    # order), so agents inherit it.
    from app.agents.registry import discover_agents
    from app.demo import refuse_default_secret_unless_demo, warn_if_provider_key_present
    from app.services import pii_service

    # The moment this process began serving, as the boot reconciliation
    # below sees it (blueprint S7, gap H16): before discovery and the
    # detector warm-up, so every run this process drives is written
    # after it. UTC, because the column it is compared with is
    # ``timestamptz``.
    boot_started_at = datetime.now(timezone.utc)

    # Blueprint S3 (L22): the shipped default secret serves only the demo,
    # and the demo announces itself. Raising here makes uvicorn exit
    # ("Application startup failed") instead of signing tokens with a
    # public value.
    refuse_default_secret_unless_demo()
    # K6 (D33): the secrets store's key is parsed before anything serves.
    # A malformed entry refuses boot here, naming its position and never
    # the key — a key that parsed as nothing would turn every secret write
    # into a 503 and every read into a silent fallback. A blank one is
    # "unconfigured": one line, and the backend serves.
    from app.services.secrets_service import check_store_key

    check_store_key()
    warn_if_provider_key_present()
    # Blueprint S4c (gap H15): build the PII detector and prove it
    # answers BEFORE the first request, so /health reports a state that
    # was measured rather than one assumed, and so the first intake of
    # the day is not the thing that discovers a missing spaCy model.
    # It never raises — an unavailable detector is a state to report and
    # to refuse requests on, not a reason to refuse to boot: a chassis
    # that will not start says nothing at all, and /health saying
    # "unavailable" is what an operator can act on.
    pii_service.warm_detector()
    _export_state_dir()
    discover_agents()
    # Blueprint S4a: the gateway is a separate process and cannot read
    # the registry discovery just filled, so the validated manifests
    # become rows here — and every row this pass did not touch is
    # stamped absent, which is how an uninstalled agent stops
    # authorizing model calls.
    #
    # A database that is not up yet must not stop the API from booting,
    # and that was the whole of it: one attempt, a warning, and nothing
    # further. The warning even said "until this succeeds it sees the
    # previous boot's rows" — true, and I had left nothing that could
    # ever make it succeed. So an agent uninstalled or stripped of its
    # `llm` grant went on authorizing model calls from the previous
    # boot's row for as long as the process lived, and a run token left
    # in Redis by the restart carried that authority to its TTL (Codex
    # P1). Availability was the right call; giving up was not.
    #
    # It retries in the background now until the CURRENT discovery is
    # the persisted one. The task is cancelled at shutdown.
    reconcile_task = asyncio.create_task(_reconcile_until_persisted())
    # Blueprint S7 (gap H16): a phase is a background task and died with
    # the previous process; a run it left ``refining`` or
    # ``investigating`` says so now — ``error`` with the reason, one
    # audit row and one platform-plane log line each — instead of
    # saying it is running forever. Same retry shape as the manifest
    # reconcile, same cancellation at shutdown.
    orphans_task = asyncio.create_task(
        _reconcile_orphans_until_done(boot_started_at)
    )
    await get_redis()
    # Clear any stale restart-required flag now that we've booted with the
    # current effective cors_origins.
    try:
        await app_settings_service.clear_cors_restart_flag()
    except Exception:
        pass
    yield
    reconcile_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reconcile_task
    orphans_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await orphans_task
    # Drain the BatchSpanProcessor queue so in-flight spans reach the OTLP
    # endpoint before the process exits. Safe even if OTEL was never
    # initialized (no-op on the default NoOpTracerProvider). Failures here
    # must not block shutdown.
    try:
        from opentelemetry import trace as _trace_api

        provider = _trace_api.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=5000)
    except Exception as e:
        logger.warning(
            "otel_shutdown_flush_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
    # The UX plane has its own providers (service.name=librerun-web) —
    # flush them too so late browser batches survive a shutdown. No-op
    # when the relay never emitted (blank endpoint).
    try:
        from app.observability.web_telemetry import get_web_telemetry

        get_web_telemetry().force_flush(timeout_millis=5000)
    except Exception as e:
        logger.warning(
            "web_telemetry_shutdown_flush_failed",
            error=str(e),
            error_type=type(e).__name__,
        )
    await close_redis()


app = FastAPI(title="LibreRun", version=__version__, lifespan=lifespan)

# CORS origins are resolved at import time from the settings service so that
# any DB override applies on the next rolling restart. Editing the setting via
# the Admin UI sets ``cors_restart_required`` (see /health) to signal that a
# restart is needed.
import asyncio as _asyncio  # noqa: E402  (localized to avoid polluting module surface)

try:
    _effective_cors_origins = _asyncio.run(_load_cors_origins())
except RuntimeError:
    # Already inside a running loop (e.g. certain test harnesses) — fall back.
    _effective_cors_origins = settings.cors_origins_list

# Innermost of the user middlewares (first add_middleware call): the
# transaction boundary (gap H6). It commits the request's DB sessions on
# ``http.response.start`` — after the handler returned, before the first
# byte leaves — so a client's very next request sees the write. See
# app/middleware_transaction.py and app/database.py.
from app.middleware_transaction import TransactionBoundaryMiddleware  # noqa: E402

app.add_middleware(TransactionBoundaryMiddleware)


# Blueprint S4c (gap H15): the PII detector's refusal, in one place.
#
# ``pii_service.redact`` raises ``PiiDetectorUnavailable`` from wherever
# the chassis is about to persist or export text it could not fully
# walk. A handler here — rather than a try/except in each endpoint — is
# what makes the policy hold for an endpoint nobody has written yet: a
# route that redacts and forgets to catch answers 503, not 500, and a
# route that forgets to redact at all is the only way past it.
#
# 503 and not 500 because this IS what the status code is for: the
# request was fine, the dependency it needs is not, and it may work on
# a retry once an operator has fixed the deployment.
from app.services.pii_service import PiiDetectorUnavailable  # noqa: E402


@app.exception_handler(PiiDetectorUnavailable)
async def _pii_detector_unavailable_handler(_request, exc: PiiDetectorUnavailable):
    # The state and the attach point, and a sentence a user can act on.
    # Never the text the caller sent, and never the exception's own
    # message beyond the class it carries.
    logger.error(
        "pii_detector_refused_request",
        stage=exc.stage,
        state=exc.state,
        error=exc.error,
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": (
                "PII redaction is unavailable, so this request was refused "
                "rather than stored unredacted. Contact your administrator."
            ),
            "code": exc.code,
            "pii_detector": {"state": exc.state, "stage": exc.stage},
        },
        headers={"Retry-After": "30"},
    )


# K6 (D14, D33): a secret write with no store key configured, in one place
# for the same reason as the handler above — every endpoint that writes
# through the secrets service answers it the same way, including the ones
# K8a adds. 503 because the request was fine and the deployment is missing
# a key an operator can add. The body is the class's own sentence: the
# exception is raised before any value is touched, and carries none.
from app.services.secrets_service import SecretsStoreUnconfigured  # noqa: E402


@app.exception_handler(SecretsStoreUnconfigured)
async def _secrets_store_unconfigured_handler(_request, exc: SecretsStoreUnconfigured):
    logger.warning("secrets_store_refused_write", code=exc.code)
    return JSONResponse(
        status_code=503,
        content={"detail": exc.detail, "code": exc.code},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_effective_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Starlette runs middlewares in reverse of the order they're added — add_middleware
# calls wrap the existing app, so the LAST add_middleware call becomes the
# outermost middleware. Register LoggingContextMiddleware after CORSMiddleware so
# it wraps CORS and sees the original request path before any CORS rewriting.
from app.middleware_logging import LoggingContextMiddleware  # noqa: E402

app.add_middleware(LoggingContextMiddleware)


app.include_router(auth_router.router, prefix="/api/v1")
app.include_router(runs_router.router, prefix="/api/v1")
app.include_router(files_router.router, prefix="/api/v1")
app.include_router(feedback_router.router, prefix="/api/v1")
app.include_router(admin_router.router, prefix="/api/v1")
app.include_router(reports_router.router, prefix="/api/v1")
app.include_router(agents_router.router, prefix="/api/v1")
app.include_router(mcp_router.router, prefix="/api/v1")
app.include_router(ux_telemetry_router.router, prefix="/api/v1")
app.include_router(otlp_relay_router.router, prefix="/api/v1")

# OTEL init MUST run at import time, after every add_middleware /
# include_router above and before uvicorn's first ASGI call: Starlette
# builds and caches the middleware stack on that first call (the lifespan
# event), and FastAPIInstrumentor instruments by patching
# ``app.build_middleware_stack`` — initializing any later would leave
# request spans permanently un-instrumented. With
# OTEL_EXPORTER_OTLP_ENDPOINT unset (and OTEL_DEBUG off) this is a
# logged no-op.
from app.observability.otel_init import init_otel  # noqa: E402

init_otel(app)


# The public repository: README.md's clone URL, less `.git`, its owner
# the maintainer's own account. `scripts/resolve_public_owner.sh` wrote it
# here and at every other site together, and `test_source_access.py`
# holds this line to README.md's.
PUBLIC_REPOSITORY_URL = "https://github.com/JeremiahJRRoss/librerun"


def source_url(configured: str) -> str:
    """Where the source of the running version is (K blueprint A1, R17).

    The operator's ``LIBRERUN_SOURCE_URL`` when it is set. Blank — what
    compose's ``${LIBRERUN_SOURCE_URL:-}`` delivers when ``.env`` says
    nothing, or says it blank — means the public repository at the running
    version's tag, ``v`` + ``__version__``: the source of what is running,
    never ``main``, which moves on without it. Derived here, at request
    time, so no file carries a version literal a version bump could miss.
    """
    return configured.strip() or f"{PUBLIC_REPOSITORY_URL}/tree/v{__version__}"


@app.get("/api/v1/meta")
async def meta(db: AsyncSession = Depends(get_db)) -> dict:
    """Public deployment facts (blueprint S3): what the login page and the
    demo banner need before anyone signs in. Carries no secret and no
    per-tenant data — the agent list is the catalogue (ids and names),
    never a run.

    ``license`` and ``source_url`` say which licence this LibreRun is under
    and where the source of the version it runs can be fetched (K blueprint
    A1, R17), so that anyone using it over a network can find what AGPL-3.0
    section 13 offers them: ``LIBRERUN_SOURCE_URL`` when the operator sets
    it, and otherwise the public repository at the running version's tag.
    """
    from app import config as _config
    from app.agents.registry import get_manifest, list_agents
    from app.demo import secret_is_default
    from app.observability.trace_viewer import effective_viewer, viewer_configured

    s = _config.settings
    agents = []
    for agent in list_agents():
        manifest = get_manifest(agent.agent_id)
        agents.append(
            {
                "id": agent.agent_id,
                "name": manifest.name if manifest is not None else agent.agent_id,
            }
        )
    viewer, base_url, template, source = await effective_viewer(db)
    # Keyless mode is the GATEWAY's to know from blueprint S4a: the switch
    # and the provider keys moved there together, and this process holds
    # no gateway credential — which is why /healthz is unauthenticated
    # and says two things. ``null`` with gateway "unreachable" when it is
    # down, because "not stubbed" would be a guess the banner acts on.
    gateway_health = await _gateway_health()
    return {
        "name": app.title,
        "version": app.version,
        "license": __license__,
        "source_url": source_url(s.LIBRERUN_SOURCE_URL),
        "demo": bool(s.LIBRERUN_DEMO),
        "stub_llm": (
            bool(gateway_health.get("stub")) if gateway_health is not None else None
        ),
        "gateway": "ok" if gateway_health is not None else "unreachable",
        # True only in demo mode (the lifespan refuses it elsewhere): the
        # banner says it only when it is so.
        "default_secret": secret_is_default(s),
        # Whether "View trace" links render, which preset renders them,
        # and whether that is the environment's configuration or an admin
        # override at runtime ("env" | "runtime"). The viewer's URL is
        # not a public fact and is not here (Codex on PR #51).
        "trace_viewer_configured": viewer_configured(viewer, base_url, template),
        "trace_viewer": viewer,
        "trace_viewer_source": source,
        "agents": agents,
    }



# The gateway's /healthz, briefly cached: /api/v1/meta is polled by every
# page load and the answer changes only when the deployment is
# reconfigured.
_GATEWAY_HEALTH_TTL_SECONDS = 10.0
_gateway_health_cache: tuple[float, dict | None] | None = None


async def _gateway_health() -> dict | None:
    global _gateway_health_cache
    import time as _time

    now = _time.monotonic()
    if (
        _gateway_health_cache is not None
        and now - _gateway_health_cache[0] < _GATEWAY_HEALTH_TTL_SECONDS
    ):
        return _gateway_health_cache[1]
    from app.services import gateway_client

    health = await gateway_client.health()
    _gateway_health_cache = (now, health)
    return health


@app.get("/api/v1/health")
async def health():
    from app.services import pii_service

    try:
        cors_restart = await app_settings_service.cors_restart_required()
    except Exception:
        cors_restart = False
    detector = pii_service.detector_status()
    return {
        "status": "ok",
        "env": settings.APP_ENV,
        "cors_restart_required": cors_restart,
        # Blueprint S4c: the readiness of the PII detector, warmed in the
        # lifespan above. ``state`` is one of ready | unavailable |
        # failed and ``coverage`` is ner | regex_only.
        #
        # This endpoint is unauthenticated, so what is here was chosen
        # rather than dumped: the state, the coverage, the failing
        # exception's CLASS and a count. A class name and a count say
        # "the detector is broken", which ``state`` already says out
        # loud; no message, no path and no text of any kind, because a
        # Presidio exception message can quote a model path and a
        # redaction's input never leaves this process at all.
        "pii_detector": detector.as_dict(),
    }
