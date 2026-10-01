"""The gateway service: OpenAI-compatible ingress for every agent.

``/healthz`` is unauthenticated and says two things — that the process
is up, and whether it is in keyless (stub) mode — because the backend's
``/api/v1/meta`` sources its ``stub_llm`` field from here and holds no
gateway credential with which to ask anything more.

Everything else needs a credential (``gateway/auth.py``).
"""
from __future__ import annotations

# The log walk goes in before the imports below, not after: LiteLLM and
# the OpenTelemetry packages log at import, and a record emitted before
# the pipeline exists is a record no pipeline can walk. Same reason and
# same placement as ``configure_logging()`` in the backend's main.
from gateway import logs  # isort: skip

logs.configure()  # isort: skip

import asyncio  # noqa: E402
import json  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

import structlog  # noqa: E402
from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from gateway import (  # noqa: E402
    auth,
    db,
    egress,
    errors,
    keys,
    progress,
    provider_store,
    redaction,
    steps,
)
from gateway.config import settings  # noqa: E402
from gateway.version import __version__  # noqa: E402

# The chassis module this image ships (see the Dockerfile): the SAME
# redaction pipeline and the SAME detector state the backend runs.
from app.services import pii_service  # noqa: E402

logger = structlog.get_logger(__name__)

# How long to wait for the database at boot before giving up. Compose
# starts services in parallel, and a gateway that exits because Postgres
# was two seconds behind would fail every `up` on a cold machine.
_DB_WAIT_SECONDS = 60


async def _reconcile_keys_at_boot() -> None:
    """Register the agent keys the environment carries.

    A variable that cannot be read as one agent's key stops the boot with
    the variable named: starting anyway would leave a container holding a
    key the gateway refuses, which looks like a model outage rather than
    a typo in ``.env``.
    """
    # Parse first, before touching the database: a malformed variable is
    # the operator's mistake and should be reported in the first second,
    # not after a minute of waiting for Postgres.
    entries = keys.parse_key_variables()
    deadline = asyncio.get_running_loop().time() + _DB_WAIT_SECONDS
    delay = 0.5
    while True:
        try:
            async with db.sessionmaker()() as session:
                report = await keys.reconcile_env_keys(session)
                await session.commit()
            logger.info(
                "gateway_agent_keys_registered",
                agents=sorted(set(report.registered)),
                count=len(set(report.registered)),
                variables=len(entries),
            )
            return
        except Exception as exc:  # noqa: BLE001
            if asyncio.get_running_loop().time() >= deadline:
                raise
            logger.warning(
                "gateway_agent_keys_waiting_for_database",
                error=str(exc),
                error_type=type(exc).__name__,
                retry_in_seconds=delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 5.0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.services import pii_service
    from gateway import telemetry

    telemetry.init()
    # Blueprint S4c (gap H15): this image ships `backend/app` and the
    # outbound redaction below runs the chassis's own `pii_service`, so
    # the detector's readiness is this process's business too. Warmed
    # here for the same reason the backend warms it — /healthz reports a
    # state that was measured, and the first model call of the day is
    # not what discovers a missing spaCy model.
    pii_service.warm_detector()
    await _reconcile_keys_at_boot()
    # K7: the provider keys pasted in the admin UI, and the sealing
    # keypair they are sealed to. After the agent keys, so the database
    # has already been waited for; a malformed store key, a key shared
    # with the backend or a keypair no key opens stops the boot here.
    await provider_store.start()
    logger.info(
        "gateway_started",
        stub=bool(settings.LIBRERUN_STUB_LLM),
        kb_embed_model=settings.LIBRERUN_KB_EMBED_MODEL,
    )
    yield
    await provider_store.stop()
    telemetry.shutdown()
    await db.dispose()


app = FastAPI(title="LibreRun Gateway", version=__version__, lifespan=lifespan)
app.add_exception_handler(errors.GatewayError, errors.gateway_error_handler)


@app.exception_handler(pii_service.PiiDetectorUnavailable)
async def _pii_detector_unavailable(_request, exc: pii_service.PiiDetectorUnavailable):
    """Blueprint S4c: outbound redaction is the last thing between an
    agent's prompt and somebody else's model. When the detector cannot
    run, this process refuses the call in the OpenAI error envelope its
    callers already parse — rather than letting the exception become a
    500 that an SDK reads as "the gateway is broken, retry".
    """
    logger.error(
        "gateway_pii_detector_refused",
        stage=exc.stage,
        state=exc.state,
        error=exc.error,
    )
    # The code is a LITERAL here, not ``exc.code``, and deliberately:
    # `test_refusal_catalogue.py` derives §10 of the authoring page from
    # the gateway's own source, and a code forwarded from a chassis
    # attribute is one it cannot see — an undocumented refusal is exactly
    # what that check exists to prevent. The literal and the chassis
    # constant are pinned equal by `test_pii_detector_state.py`.
    refusal = errors.unavailable(
        "pii_detector_unavailable",
        "PII redaction is unavailable, so this request was refused rather "
        "than sent to a provider unredacted.",
    )
    return await errors.gateway_error_handler(_request, refusal)


@app.get("/healthz")
async def healthz() -> dict:
    """Unauthenticated, and deliberately three fields.

    The backend's ``/api/v1/meta`` reads this to report keyless mode, and
    it presents no credential to do so. Anything else here — a model
    list, a provider name, an agent count — would be an unauthenticated
    disclosure about the deployment. What the gateway holds is written to
    the ``gateway_status`` row instead (K7, D16), which only the
    platform-admin endpoints read; this stays the liveness probe.
    """
    detector = pii_service.detector_status()
    return {
        "status": "ok",
        "stub": bool(settings.LIBRERUN_STUB_LLM),
        # Blueprint S4c. Two fields, the same two the backend's /health
        # carries, and for the same reason it carries them: outbound
        # redaction runs here, so "is the detector ready" is a question
        # about THIS process and cannot be answered by asking the
        # backend. Nothing further — no exception class here, because
        # unlike the backend's /health this endpoint is reachable by
        # every agent container on the network.
        "pii_detector": {"state": detector.state, "coverage": detector.coverage},
    }


@app.get("/v1/models")
async def list_models(
    request: Request, session: AsyncSession = Depends(db.get_db)
) -> dict:
    """The steps this agent may name, in OpenAI's model-list shape.

    The one endpoint an agent key authenticates alone: it discloses the
    calling agent's own declared steps and nothing about any tenant, so
    the run token it cannot present would add nothing.
    """
    principal = await auth.authenticate(request, session)
    principal.require("llm")
    return {
        "object": "list",
        "data": [
            {
                "id": f"librerun/{step['id']}",
                "object": "model",
                "owned_by": "librerun",
                "created": 0,
            }
            for step in principal.snapshot.steps
        ],
    }


# --------------------------------------------------------------------------
# The model calls. One shape for both: authenticate, resolve the step,
# redact, call, record. Every refusal happens before anything leaves the
# box, which is why the redaction runs before the egress and not around
# it.
# --------------------------------------------------------------------------


async def _body_of(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise errors.bad_request("invalid_json", "the request body is not JSON")
    if not isinstance(body, dict):
        raise errors.bad_request("invalid_json", "the request body is not an object")
    return body


def _resolved(response: dict, step) -> dict:
    """Tell the caller WHICH provider and model actually answered.

    The reply is OpenAI-shaped and OpenAI-shaped replies do not name a
    provider, so a caller could only fall back to the manifest's
    packaged default — which is wrong for exactly the tenants this batch
    exists to serve, the ones who overrode it. An agent's schema-drift
    audit rows were being attributed that way (Codex P2).

    It travels in ``librerun``, the platform's own envelope, which until
    now went only one way (the keyless fixture, inbound). Making it
    bidirectional keeps every LibreRun-specific field in one named place
    instead of inventing a second convention, and an OpenAI client that
    does not know the key ignores it.

    Non-streamed replies only: a streamed one is passed through chunk by
    chunk, verbatim, and an extra key injected into those chunks would
    be the gateway corrupting the very format it promises to speak.
    """
    if isinstance(response, dict):
        response["librerun"] = {
            # The admin's own word, not LiteLLM's routing prefix: this is
            # what the configuration page shows and what an audit row
            # should say.
            "provider": step.provider,
            "model": step.model,
            "step_id": step.step_id,
        }
    return response


@app.post("/v1/chat/completions")
async def chat_completions(
    request: Request, session: AsyncSession = Depends(db.get_db)
):
    principal = await auth.authenticate(request, session)
    auth.require_model_call(principal)
    body = await _body_of(request)
    step = await steps.resolve(
        session,
        principal,
        step_header=request.headers.get(auth.STEP_HEADER),
        model=body.get("model"),
    )
    # The last line of this route that needs the database. Everything
    # after it — the redaction walk, the provider call, a stream that
    # may run for minutes — held the transaction and the key row's lock
    # open for no reason (Codex P1); see ``db.release``.
    await db.release(session)
    outbound, report = redaction.redact_request(
        body,
        tenant_id=principal.tenant_id,
        enabled=principal.snapshot.redact_outbound,
    )
    scenario = request.headers.get(auth.SCENARIO_HEADER)
    traceparent = request.headers.get("traceparent")
    wants_stream = bool(body.get("stream"))

    from gateway import telemetry

    if wants_stream:
        return await _stream_completion(
            principal, step, outbound, report,
            scenario=scenario, traceparent=traceparent,
        )

    with telemetry.llm_span(
        principal, step, operation="chat", traceparent=traceparent
    ) as span:
        telemetry.record_prompt(span, outbound.get("messages"))
        _log_redaction(principal, step, report)
        response = await egress.complete(
            outbound,
            step,
            scenario=scenario,
            # Read HERE, not at authentication: the redaction walk above
            # has already spent part of the budget (~2s on a large
            # request), and a snapshot would hand the provider an
            # allowance that was true a moment ago.
            seconds_left=principal.seconds_left(),
        )
        cost = egress.cost_usd(response, step)
        telemetry.record_response(span, response, step, cost, operation="chat")
    await progress.record_model(principal, step, response)
    return _resolved(response, step)


class _StreamAssembly:
    """The streamed deltas, put back together for the span.

    The client gets the chunks verbatim; this exists only so
    ``record_response`` sees what the model actually said. It was one
    list of ``delta.content`` and one finish reason, which meant a
    tool-calling stream recorded an EMPTY completion — the whole answer
    is in ``tool_calls`` deltas — and ``n > 1``, which the gateway
    allows, merged every choice into a fabricated choice 0 (Codex P2). A
    span that quietly says "the model returned nothing" is worse than no
    span: it is evidence, and it was wrong.

    Deltas are accumulated per choice index, and tool-call deltas per
    index within that choice, which is how the wire format is defined:
    the id and name arrive once, the arguments arrive in pieces.
    """

    def __init__(self) -> None:
        self._choices: dict[int, dict] = {}

    def _choice(self, index: int) -> dict:
        return self._choices.setdefault(
            index,
            {
                "content": [],
                "role": None,
                "finish_reason": None,
                "calls": {},
                # A safety refusal streams in pieces like content, in
                # ``delta.refusal``, and is the whole answer when it
                # appears — ``content`` stays null.
                "refusal": [],
                # An audio reply streams its transcript the same way, and
                # its base64 ``data`` in pieces we only ever measure.
                "audio": {"transcript": [], "bytes": 0},
                # The legacy spelling, streamed the same way: a name
                # once, arguments in pieces.
                "legacy": {"name": None, "arguments": []},
            },
        )

    def add(self, chunk: dict) -> None:
        for raw in chunk.get("choices") or []:
            if not isinstance(raw, dict):
                continue
            entry = self._choice(int(raw.get("index") or 0))
            if raw.get("finish_reason"):
                entry["finish_reason"] = raw["finish_reason"]
            delta = raw.get("delta") or {}
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("role"), str):
                entry["role"] = delta["role"]
            if isinstance(delta.get("content"), str):
                entry["content"].append(delta["content"])
            if isinstance(delta.get("refusal"), str):
                entry["refusal"].append(delta["refusal"])
            audio = delta.get("audio")
            if isinstance(audio, dict):
                if isinstance(audio.get("transcript"), str):
                    entry["audio"]["transcript"].append(audio["transcript"])
                if isinstance(audio.get("data"), str):
                    entry["audio"]["bytes"] += len(audio["data"])
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                slot = entry["calls"].setdefault(
                    int(call.get("index") or 0),
                    {"id": None, "type": "function", "name": None, "arguments": []},
                )
                if call.get("id"):
                    slot["id"] = call["id"]
                if call.get("type"):
                    slot["type"] = call["type"]
                function = call.get("function") or {}
                if isinstance(function, dict):
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if isinstance(function.get("arguments"), str):
                        slot["arguments"].append(function["arguments"])
            legacy = delta.get("function_call")
            if isinstance(legacy, dict):
                if legacy.get("name"):
                    entry["legacy"]["name"] = legacy["name"]
                if isinstance(legacy.get("arguments"), str):
                    entry["legacy"]["arguments"].append(legacy["arguments"])

    def choices(self) -> list[dict]:
        out = []
        for index in sorted(self._choices):
            entry = self._choices[index]
            message: dict = {
                "role": entry["role"] or "assistant",
                "content": "".join(entry["content"]) or None,
            }
            if entry["refusal"]:
                message["refusal"] = "".join(entry["refusal"])
            if entry["audio"]["transcript"] or entry["audio"]["bytes"]:
                message["audio"] = {
                    "transcript": "".join(entry["audio"]["transcript"]),
                    # The COUNT, never a reconstruction of the payload.
                    # The obvious shortcut here is to hand the walker a
                    # filler string of the right length so it can measure
                    # it the usual way — which allocates megabytes to
                    # learn a number already in hand, on every audio
                    # reply. ``_walked_audio`` takes the count directly.
                    "bytes": entry["audio"]["bytes"],
                }
            if entry["legacy"]["name"] or entry["legacy"]["arguments"]:
                message["function_call"] = {
                    "name": entry["legacy"]["name"],
                    "arguments": "".join(entry["legacy"]["arguments"]),
                }
            if entry["calls"]:
                message["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": call["type"],
                        "function": {
                            "name": call["name"],
                            "arguments": "".join(call["arguments"]),
                        },
                    }
                    for _, call in sorted(entry["calls"].items())
                ]
            out.append(
                {
                    "index": index,
                    "finish_reason": entry["finish_reason"],
                    "message": message,
                }
            )
        return out or [
            {"index": 0, "finish_reason": None, "message": {"role": "assistant", "content": None}}
        ]


async def _stream_completion(
    principal, step, outbound, report, *, scenario, traceparent
):
    """Streaming keeps the same span open across the whole stream.

    The usage — and therefore the cost — arrives in the final chunk, so
    the span cannot be closed when the first byte leaves. The generator
    owns the span's lifetime for that reason.

    The provider call is STARTED before the response is returned
    (``egress.open_stream``), so a refusal on the way in is still a
    status rather than a 200 whose stream ends before its first event.
    The span is opened inside the generator, in the response's own
    task — and it is stamped with the moment the call actually began, so
    splitting the setup out does not shorten it.

    Two things have to happen BEFORE that call, and both were casualties
    of moving it (Codex P2, on the fix that moved it):

    - **The trace check.** ``parent_context`` is what refuses a
      ``traceparent`` naming somebody else's trace, and it used to run
      first because ``llm_span`` opened before the call. With the call
      moved ahead of the span, a foreign header no longer stopped the
      prompt reaching the provider: the request was accepted and billed,
      and the 403 arrived after a 200 had been committed. It is called
      here, for its refusal, before anything leaves.
    - **A span for a failed attempt.** A refusal from ``open_stream``
      exits before the generator ever runs, so there was no LLM span at
      all — a deployment whose credential is wrong left nothing in the
      run's trace, while the non-streaming path records exactly that.
      The failure gets its own span, opened AND closed in this task so
      no context token crosses one.
    """
    import time

    from gateway import telemetry

    # For the refusal only. The span opens its own context from the same
    # header, in the response's task; this is a pure call and repeating
    # it there costs one parse.
    telemetry.parent_context(principal, traceparent)

    started = time.time_ns()
    try:
        iterator = await egress.open_stream(
            outbound,
            step,
            scenario=scenario,
            seconds_left=principal.seconds_left(),
            # A CALLABLE for the iteration: setting the call up costs
            # time too, and a stream is read after that.
            remaining=principal.seconds_left,
        )
    except errors.GatewayError:
        with telemetry.llm_span(
            principal,
            step,
            operation="chat",
            traceparent=traceparent,
            start_time=started,
        ):
            raise

    async def body():
        aggregate = {"choices": [], "usage": {}, "model": step.model}
        with telemetry.llm_span(
            principal,
            step,
            operation="chat",
            traceparent=traceparent,
            start_time=started,
        ) as span:
            telemetry.record_prompt(span, outbound.get("messages"))
            _log_redaction(principal, step, report)
            assembly = _StreamAssembly()
            async for chunk in iterator:
                assembly.add(chunk)
                if chunk.get("usage"):
                    aggregate["usage"] = chunk["usage"]
                if chunk.get("model"):
                    aggregate["model"] = chunk["model"]
                yield f"data: {json.dumps(chunk, default=str)}\n\n"
            aggregate["choices"] = assembly.choices()
            cost = egress.cost_usd(aggregate, step)
            telemetry.record_response(span, aggregate, step, cost, operation="chat")
        await progress.record_model(principal, step, aggregate)
        yield "data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


@app.post("/v1/embeddings")
async def embeddings(request: Request, session: AsyncSession = Depends(db.get_db)):
    principal = await auth.authenticate(request, session)
    auth.require_model_call(principal)
    body = await _body_of(request)
    step = await steps.resolve(
        session,
        principal,
        step_header=request.headers.get(auth.STEP_HEADER),
        model=body.get("model"),
        embedding=True,
    )
    if step.platform:
        raw = body.get("input")
        values = raw if isinstance(raw, list) else [raw]
        # Unfiltered: dropping the non-strings here counted them as no
        # input at all, so the bound went unenforced for exactly the
        # shape it cannot measure (Codex P2).
        steps.check_kb_embed_bounds(values)
    # As on the chat route: nothing below this line touches the
    # database, so nothing below it should hold a connection or a lock.
    await db.release(session)
    outbound, report = redaction.redact_request(
        body,
        tenant_id=principal.tenant_id,
        enabled=principal.snapshot.redact_outbound,
        embedding=True,
    )

    from gateway import telemetry

    with telemetry.llm_span(
        principal, step, operation="embeddings", traceparent=request.headers.get("traceparent")
    ) as span:
        _log_redaction(principal, step, report)
        response = await egress.embed(outbound, step, seconds_left=principal.seconds_left())
        cost = egress.cost_usd(response, step)
        telemetry.record_response(
            span, response, step, cost, operation="embeddings"
        )
    await progress.record_model(principal, step, response)
    return _resolved(response, step)


def _log_redaction(principal, step, report) -> None:
    """One line per call, paths and counts only — never a value."""
    if not report.changed:
        return
    logger.info(
        "llm_outbound_redacted",
        agent_id=principal.agent_id,
        step_id=step.step_id,
        redacted=len(report.redacted_paths),
        dropped=report.dropped_paths,
        paths=report.redacted_paths,
    )
