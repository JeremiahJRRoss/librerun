"""Generic agent runner — background-task entrypoint for any registered agent.

Owns all snapshot + run-status writes. Agents return pure results
(``AnalysisResult`` / ``InvestigationResult``); this runner persists the
generic agent columns (``analysis``, ``structured_data``, ``report_html``).

Blueprint B7: the phase list is data. The runner walks the agent's
manifest (``agent.yaml``) instead of hardcoding an analyze/investigate
pair:

- ``start_run``   — run submitted; run from the first phase.
- ``resume_run``  — human approved; run from the phase after
  ``run.current_phase``.
- ``rerun_current_phase`` — human edited the output under review; run
  ``run.current_phase`` again with ``user_edits``.

Consecutive phases without an ``approval`` gate auto-advance inside one
background task; a gated phase parks the run in ``awaiting_approval``
until the approve endpoint calls ``resume_run``. The last phase's
result lands in ``structured_data``/``report_html`` and completes the
run; earlier phases land in ``analysis``.

Each phase wraps the agent invocation in an OpenInference ``CHAIN`` span
whose parent is the run's root ``run`` span (blueprint S4, promise 3):
the submission request opened that root and persisted its W3C context on
the run row, and every phase — in whatever process, however long after an
approval — starts with the pair parsed back as its remote parent, so a
gated run is one trace tree and the trace id never changes
(``app.observability.run_trace``).
The structure is **using_attributes outside, start_as_current_span inside**:
``using_attributes`` writes session/user/metadata into the OTEL context API,
and ``get_attributes_from_context`` then re-merges them onto the manual
phase span at creation time. OpenInference documents this explicitly —
context attributes set via ``using_attributes`` are NOT automatically
copied onto spans created with ``start_as_current_span``, only onto spans
created by the LLM auto-instrumentors. The re-merge is what makes the
Sessions and Users views work for phase rows in the trace tree, not just
their LLM children.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Awaitable, Callable
from uuid import UUID

import structlog
from openinference.instrumentation import (
    get_attributes_from_context,
    using_attributes,
)
from openinference.semconv.trace import (
    OpenInferenceMimeTypeValues,
    OpenInferenceSpanKindValues,
    SpanAttributes,
)
from opentelemetry import trace as _otel_trace
from datetime import datetime, timezone

from sqlalchemy import select

from app.agents.manifest import AgentManifest, PhaseSpec
from app.agents.protocol import AgentInput, StepProgress
from app.agents.registry import get_agent, get_manifest
from app.config import settings as _settings
from app.database import async_session
from app.logging_context import log_context
from app.logging_pii import user_content
from app.models import Run, RunSnapshot
from app.observability import run_trace
from app.services import run_boundary, run_errors
from app.services.audit_service import log_audit
from app.services.pii_service import PiiRefused, UnwalkableValue
from app.redis import get_redis
from app.tracing import safe_json

logger = structlog.get_logger(__name__)
_tracer = _otel_trace.get_tracer("librerun.agent_runner")

_CHAIN_KIND = OpenInferenceSpanKindValues.CHAIN.value
_JSON_MIME = OpenInferenceMimeTypeValues.JSON.value

# Result statuses the chassis reads as "this phase succeeded". Agents on
# the AgentProtocol vocabulary return ``awaiting_approval`` from non-final
# phases (the output is ready for review — whether a review actually
# happens is the *manifest's* call, not the agent's) and ``complete`` from
# the final one.
_SUCCESS_STATUSES = frozenset({"awaiting_approval", "complete"})


class PhaseDeadlineExceeded(RuntimeError):
    """One invocation of a phase outran its wall-clock budget (blueprint
    S4). The message names the phase and the deadline; the runner marks
    the run ``error`` with it."""


def phase_deadline(spec: PhaseSpec, ceiling: int | None = None) -> int:
    """The invocation's budget: the manifest's ``deadline_seconds`` under
    a ceiling, or the ceiling itself when the phase declares none. A value
    above the ceiling is clamped and logged — the ceiling is the
    operator's, not the agent's.

    ``ceiling`` defaults to this process's ``LIBRERUN_MAX_PHASE_SECONDS``,
    which is what the runner wants. A caller that IS the operator for the
    invocations it drives passes its own: the conformance battery's
    ``--timeout`` plays exactly that role for the phases it runs, and
    before it could say so it composed this function with a second clamp
    of its own — which read the ceiling of whatever process imported it
    and so could only ever lower the number, never raise it (Codex round
    13). Measured: a manifest declaring 5000s under ``--timeout 5000``
    advertised 3600. One parameter, one implementation; two copies of a
    rule is how one of them quietly stops matching."""
    ceiling = max(
        1,
        int(_settings.LIBRERUN_MAX_PHASE_SECONDS if ceiling is None else ceiling),
    )
    declared = spec.deadline_seconds
    if declared is None:
        return ceiling
    if declared > ceiling:
        logger.warning(
            "phase_deadline_clamped",
            phase=spec.name,
            declared_seconds=declared,
            ceiling_seconds=ceiling,
        )
        return ceiling
    return declared


def _phase_tags(*, agent_id: str, phase: str, run_number: str | None) -> list[str]:
    """Tags emitted via ``using_attributes`` so they propagate to every
    descendant span — including the LLM auto-instrumented children, which
    the OpenInference instrumentors stamp from the OTEL context.

    ``run:<number>`` makes "show me everything for RUN-1042" a one-line
    filter in the trace viewer across every phase trace. The prefix is
    intentional: keeps tags grep-friendly and groups visually with the
    existing ``agent:`` and ``phase:`` tags. Skipped entirely when the
    run has no number yet (shouldn't happen post-allocation, but be
    safe).
    """
    tags = [f"agent:{agent_id}", f"phase:{phase}"]
    if run_number:
        tags.append(f"run:{run_number}")
        # Pre-S1 spelling, kept for one release so saved trace-viewer
        # filters keep matching; dropped at v1.1 (blueprint S1, L18).
        tags.append(f"case:{run_number}")
    return tags


def _phase_span_attrs(
    *,
    run_id: UUID,
    tenant_id: UUID,
    run_number: str | None,
    agent_id: str,
    phase: str,
    user_inputs: dict | None,
) -> dict:
    """Build the attribute dict for a phase root span at creation time.

    Sampler decisions only see attributes present at span creation, which
    is why we assemble these eagerly rather than calling ``set_attribute``
    inside the ``with`` block.
    """
    attrs = dict(get_attributes_from_context())
    attrs[SpanAttributes.OPENINFERENCE_SPAN_KIND] = _CHAIN_KIND
    attrs[SpanAttributes.INPUT_VALUE] = safe_json(user_inputs or {})
    attrs[SpanAttributes.INPUT_MIME_TYPE] = _JSON_MIME
    attrs["run_id"] = str(run_id)
    attrs["tenant_id"] = str(tenant_id)
    attrs["run_number"] = run_number or ""
    # Pre-S1 spellings, duplicated for one release so saved trace queries
    # keep matching; dropped at v1.1 (blueprint S1, L18).
    attrs["case_id"] = str(run_id)
    attrs["case_number"] = run_number or ""
    attrs["agent_id"] = agent_id
    attrs["phase"] = phase
    return attrs


def _on_progress(redis, run_id: UUID) -> Callable[[StepProgress], Awaitable[None]]:
    """Return a progress callback matching ``PipelineOrchestrator.update_progress``.

    Provided for protocol conformance — agents that use the orchestrator
    (like the demo agent) write progress themselves. Agents that don't use the
    orchestrator should call this callback directly.
    """

    async def _write(p: StepProgress) -> None:
        # The one progress write path (blueprint S4): the step id is the
        # hash's field name and is refused when flagged
        # (``pii_in_progress``), the detail is redacted.
        await run_boundary.progress_write(
            redis, run_id, p.step_id, p.status, p.detail
        )

    return _write


async def _get_or_create_snapshot(db, run: Run) -> RunSnapshot:
    result = await db.execute(
        select(RunSnapshot).where(RunSnapshot.run_id == run.id)
    )
    snapshot = result.scalar_one_or_none()
    if snapshot is None:
        snapshot = RunSnapshot(run_id=run.id, tenant_id=run.tenant_id)
        db.add(snapshot)
        await db.flush()
    return snapshot


def _set_error(run: Run, code: str, detail: object) -> None:
    """The one way a run becomes ``error`` (blueprint S7): the status, a
    code from the chassis's closed vocabulary — what the customer page
    turns into a sentence — and the operator-facing detail, redacted
    before it is stored (``run_errors.safe_detail``). Every error site
    in this module lands here so none of them can leave the reason
    behind in a log line, which is what every one of them did."""
    run.status = "error"
    run.error_code = code
    run.error_detail = run_errors.safe_detail(detail)


def _classify(exc: BaseException) -> str:
    """The code for an exception that escaped a phase."""
    if isinstance(exc, PhaseDeadlineExceeded):
        return run_errors.DEADLINE_EXCEEDED
    # A container's ``failed`` event surfaces as ContainerAgentError —
    # the agent said so, in its own words, which ``safe_detail`` keeps
    # (redacted) for the admin view.
    if type(exc).__name__ == "ContainerAgentError":
        return run_errors.AGENT_FAILED
    return run_errors.PHASE_FAILED


async def _mark_error(db, run_id: UUID, code: str, detail: object) -> None:
    run = await db.get(Run, run_id)
    if run is not None:
        _set_error(run, code, detail)
        await db.commit()


async def _scrub_run_secrets(run: Run, manifest: AgentManifest) -> None:
    """Put every value the run's declared tool secrets resolve to now into
    the scrub set the runner holds (K8a, D20): at the start, and again
    before each output walk, so a value an admin replaced mid-run is
    scrubbed as well as the one it replaced. An agent that declares none
    costs no query."""
    if not manifest.secrets:
        return
    from app.services import tool_secrets_service as tool_secrets

    for value in await tool_secrets.scrub_values(
        run.tenant_id,
        manifest.id,
        manifest.secrets,
        env_fallback=manifest.runtime != "container",
    ):
        run_boundary.add_scrub(value)


@contextlib.contextmanager
def _delivered_until_the_run_ends(run_id: UUID):
    """The values the run was delivered, held while it can still write:
    dropped when this driver ends, unless the run parked for approval —
    its next phase, in a later driver, keeps what this one was delivered
    (Codex on #173)."""
    state = {"parked": False}
    try:
        yield state
    finally:
        if not state["parked"]:
            run_boundary.forget_run(run_id)


# Statuses of a run whose invocation is (supposedly) in flight. NOT
# ``awaiting_approval``: a parked run has no invocation, and waits on a
# human indefinitely by design.
NON_TERMINAL_STATUSES = ("submitted", "refining", "investigating")


async def reconcile_orphaned_runs(db, *, boot_started_at: datetime) -> list[dict]:
    """Boot reconciliation (blueprint S7, gap H16): every run that says it
    is running but whose invocation the previous process took with it
    becomes ``error`` with the reason stated.

    A phase runs as a ``BackgroundTasks`` job and dies with the process;
    nothing else revisits a run left ``refining`` or ``investigating``, so
    a restart mid-phase used to leave a run that said it was running
    forever, with no deadline in force. Durable resume is v1.1; 1.0 says
    what happened.

    "Whose current invocation started before this boot" is decided by
    the row's ``updated_at`` — a database-clock timestamp the ``runs``
    trigger sets on every write, so it is the moment the last process
    touched the run (the phase start, the approve endpoint's status
    write) — against the moment this process's lifespan began. A run
    THIS process is driving was written after that moment and is left
    alone, which matters because this runs in the background and may
    succeed minutes after boot when the database was late. The margin is
    the seconds discovery and the detector warm-up take before the API
    serves its first request, so a clock skew between database and
    backend hosts would have to exceed that to misfile a run. One
    process per deployment is assumed, as it is everywhere a background
    task is: a second replica's live runs are indistinguishable from
    orphans here.

    Platform-level by construction — it spans every tenant, like the
    manifest reconcile beside it — and each run's audit row and log
    line still name the tenant. The caller owns the transaction.
    """
    rows = (
        await db.execute(
            select(Run).where(
                Run.status.in_(NON_TERMINAL_STATUSES),
                Run.deleted_at.is_(None),
                Run.updated_at < boot_started_at,
            )
        )
    ).scalars().all()
    reconciled: list[dict] = []
    for run in rows:
        previous_status = run.status
        phase = run.current_phase
        detail = (
            f"backend restarted during phase {phase!r}"
            if phase
            else "backend restarted before the first phase started"
        )
        _set_error(run, run_errors.BACKEND_RESTARTED, detail)
        record = {
            "run_id": str(run.id),
            "run_number": run.run_number,
            "tenant_id": str(run.tenant_id),
            "agent": run.agent_id,
            "phase": phase,
            "previous_status": previous_status,
            "reason": detail,
        }
        # One audit row per run — a system event: no user, no address —
        # under the ``run_update`` action the schema's CHECK already
        # admits, with the transition named in the detail.
        await log_audit(
            db,
            run.tenant_id,
            None,
            None,
            "run_update",
            {**record, "action": "boot_reconcile"},
            None,
        )
        # One PLATFORM-plane log line per run: the plane is decided by
        # whether ``agent_id`` is bound (``logging_context.current_scope``),
        # and this is the chassis speaking about a run it is not
        # executing, so the agent is named under another key on purpose.
        logger.warning("run_orphaned_by_restart", **record)
        reconciled.append(record)
    return reconciled


def resume_status_for(agent_id: str, current_phase: str | None) -> str:
    """Status the approve endpoint should show while the resumed run works.

    ``investigating`` when the next phase is the manifest's last (the run
    will finish or error from here), ``refining`` when more gated phases
    follow. Falls back to ``investigating`` — the pre-B7 approve semantics
    — when the agent or cursor can't be resolved (the runner will surface
    the real failure).
    """
    manifest = get_manifest(agent_id)
    if manifest is None:
        return "investigating"
    idx = _resume_index(manifest, current_phase)
    if idx is None or idx >= len(manifest.phases):
        return "investigating"
    return "investigating" if idx == len(manifest.phases) - 1 else "refining"


def _resume_index(manifest: AgentManifest, current_phase: str | None) -> int | None:
    """Index of the phase to resume with after approval.

    ``current_phase`` NULL means a legacy row from before migration 009 —
    those were parked by the old two-phase runner, whose approve always
    launched the second phase. A cursor naming a phase the manifest no
    longer declares returns ``None`` (the caller marks the case errored).
    """
    if current_phase is None:
        return 1 if len(manifest.phases) > 1 else None
    idx = manifest.phase_index(current_phase)
    if idx is None:
        return None
    return idx + 1


async def start_run(run_id: UUID, tenant_id: UUID, agent_id: str) -> None:
    """Run submitted — run from the manifest's first phase."""
    await _run(run_id, tenant_id, agent_id, mode="start")


async def resume_run(run_id: UUID, tenant_id: UUID, agent_id: str) -> None:
    """Human approved the parked output — run from the next phase."""
    await _run(run_id, tenant_id, agent_id, mode="resume")


async def rerun_current_phase(
    run_id: UUID, edited_statement: str, agent_id: str
) -> None:
    """Human edited the output under review — re-run the current phase."""
    await _run(
        run_id,
        None,
        agent_id,
        mode="rerun",
        user_edits=edited_statement,
    )


async def _run(
    run_id: UUID,
    tenant_id: UUID | None,
    agent_id: str,
    *,
    mode: str,
    user_edits: str | None = None,
) -> None:
    """Shared driver behind start/resume/rerun.

    ``tenant_id`` may be ``None`` for reruns (the edit endpoint doesn't
    thread it); it is always read back off the case row, which is the
    authority anyway.
    """
    redis = await get_redis()
    # The run's tool secrets are scrubbed from everything it persists,
    # its error path included (K8a, D20): the set is the run's own in
    # this process, shared with the MCP requests its container makes,
    # and is filled once the run's tenant and manifest are read.
    with log_context(
        run_id=str(run_id), agent_id=agent_id, run_mode=mode
    ), run_boundary.scrubbing((), run_id=run_id), _delivered_until_the_run_ends(
        run_id
    ) as lifetime:
        logger.info("run_started")
        async with async_session() as db:
            try:
                run = await db.get(Run, run_id)
                if run is None:
                    return
                tenant_id = run.tenant_id
                with log_context(tenant_id=str(tenant_id)):
                    agent = get_agent(agent_id)
                    manifest = get_manifest(agent_id)
                    if agent is None or manifest is None:
                        logger.error("run_unknown_agent", agent_id=agent_id)
                        _set_error(
                            run,
                            run_errors.AGENT_UNAVAILABLE,
                            f"agent {agent_id!r} is not registered in this process",
                        )
                        await db.commit()
                        return
                    await _scrub_run_secrets(run, manifest)

                    if mode == "start":
                        start_index = 0
                    elif mode == "resume":
                        idx = _resume_index(manifest, run.current_phase)
                        if idx is None or idx >= len(manifest.phases):
                            logger.error(
                                "run_cursor_invalid",
                                current_phase=run.current_phase,
                                phases=manifest.phase_names(),
                            )
                            _set_error(
                                run,
                                run_errors.PHASE_INVALID,
                                f"cannot resume after phase {run.current_phase!r}: "
                                f"the manifest declares {manifest.phase_names()}",
                            )
                            await db.commit()
                            return
                        start_index = idx
                    else:  # rerun
                        cursor = run.current_phase or manifest.phases[0].name
                        idx = manifest.phase_index(cursor)
                        if idx is None:
                            logger.error(
                                "run_cursor_invalid",
                                current_phase=run.current_phase,
                                phases=manifest.phase_names(),
                            )
                            _set_error(
                                run,
                                run_errors.PHASE_INVALID,
                                f"cannot rerun phase {cursor!r}: the manifest "
                                f"declares {manifest.phase_names()}",
                            )
                            await db.commit()
                            return
                        start_index = idx

                    await _run_phases(
                        db,
                        redis,
                        run,
                        agent,
                        manifest,
                        start_index,
                        rerun=(mode == "rerun"),
                        user_edits=user_edits,
                    )
                    lifetime["parked"] = run.status == "awaiting_approval"
            except Exception as e:
                logger.exception("run_failed", error=user_content(str(e)))
                try:
                    await db.rollback()
                except Exception:
                    pass
                try:
                    async with async_session() as _db:
                        await _mark_error(
                            _db, run_id, _classify(e), f"{type(e).__name__}: {e}"
                        )
                except Exception:
                    pass


async def _run_phases(
    db,
    redis,
    run: Run,
    agent,
    manifest: AgentManifest,
    start_index: int,
    *,
    rerun: bool,
    user_edits: str | None,
) -> None:
    """Walk the manifest phase list from ``start_index``.

    Stops at the first approval gate (run parked ``awaiting_approval``),
    on failure (``error``), or after the final phase (``complete``).

    Every phase span starts under the run's persisted root (S4): the
    row's ``root_traceparent`` / ``root_tracestate`` parsed back as a
    remote parent — a row from before the root existed gets one minted
    here, so from now on the run is one tree either way.
    """
    phases = manifest.phases
    root_ctx = run_trace.ensure_root(run, agent_id=agent.agent_id)
    i = start_index
    from app import logging_queue as _logging_queue
    while i < len(phases):
        spec = phases[i]
        is_final = i == len(phases) - 1
        # A rerun repeats the phase whose output was under review — label
        # its telemetry accordingly so re-runs are distinguishable from
        # first attempts, mirroring the pre-B7 ``phase1_edit`` shape.
        phase_label = f"{spec.name}_edit" if rerun and i == start_index else spec.name

        with using_attributes(
            session_id=str(run.id),
            user_id=str(run.user_id),
            metadata={
                "tenant_id": str(run.tenant_id),
                "run_number": run.run_number or "",
                "agent_id": agent.agent_id,
                "agent_name": agent.display_name,
                "phase": phase_label,
            },
            tags=_phase_tags(
                agent_id=agent.agent_id,
                phase=phase_label,
                run_number=run.run_number,
            ),
        ), log_context(
            session_id=str(run.id),
            user_id=str(run.user_id),
            agent_name=agent.display_name,
            run_number=run.run_number or "",
            phase=phase_label,
        ):
            phase_attrs = _phase_span_attrs(
                run_id=run.id,
                tenant_id=run.tenant_id,
                run_number=run.run_number,
                agent_id=agent.agent_id,
                phase=phase_label,
                user_inputs=run.user_inputs,
            )
            if user_edits is not None and rerun and i == start_index:
                # The customer edit is the meaningful new input on a
                # re-run; record its length rather than the contents
                # (it's free-form user text that may include vendor
                # data).
                phase_attrs["user_edits.chars"] = len(user_edits or "")
            deadline = phase_deadline(spec)
            phase_attrs["phase.deadline_seconds"] = deadline

            # The belt beneath the queue-only invariant (blueprint S4):
            # a stray handler is removed and named before agent code runs.
            _logging_queue.enforce()
            logger.info("phase_started", approval_gated=spec.approval)
            # ``context=root_ctx`` makes the persisted root this span's
            # parent (a remote, ended parent is a valid one) — the same
            # trace id before and after an approval, and parent-child
            # says what the retired previous-phase link used to say.
            # ``None`` only when tracing is off, when the span is a no-op.
            with _tracer.start_as_current_span(
                phase_label, context=root_ctx, attributes=phase_attrs
            ) as span:
                sc = span.get_span_context()
                if sc.is_valid:
                    trace_hex = format(sc.trace_id, "032x")
                    if run.trace_id != trace_hex:
                        # Only a row whose root could not be restored or
                        # minted lands here; keep the lookup column honest.
                        logger.warning(
                            "run_trace_id_changed",
                            previous=run.trace_id,
                            current=trace_hex,
                        )
                        run.trace_id = trace_hex
                    if is_final:
                        # Feedback targets a specific span, so persist the
                        # final phase's span id for trace deep-links.
                        run.phase2_span_id = format(sc.span_id, "016x")

                run.status = "investigating" if is_final else "refining"
                run.current_phase = spec.name
                await db.commit()

                snap = await _get_or_create_snapshot(db, run)

                from app import capabilities as _capabilities
                from app.services import run_token as _run_token

                # Blueprint S4a: an in-process agent's model call has to
                # be attributable too, and the gateway is a separate
                # service that can only be told by a credential. So the
                # runner mints the invocation's token here — the phase
                # span is current, so the token carries the run's trace
                # and this phase as the parent the LLM span hangs from.
                # A container agent mints its own inside its runtime,
                # scoped to the invocation it drives; minting a second
                # one here would be a second live credential for nothing.
                facade_token: str | None = None
                facade_record: dict | None = None
                if manifest.runtime != "container":
                    headers = run_trace.propagation_headers()
                    facade_token = _run_token.new_token()
                    facade_record = _run_token.build_record(
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        agent_id=manifest.id,
                        grants=list(manifest.capabilities),
                        user_id=run.user_id,
                        run_number=run.run_number,
                        deadline_seconds=deadline,
                        trace_id=run_trace.current_trace_id(),
                        traceparent=headers.get(run_trace.TRACEPARENT_HEADER),
                    )
                    await _run_token.register(redis, facade_token, facade_record)

                if "kb" in manifest.capabilities:
                    # K8a (D36): ``caps.kb.available()`` is synchronous and
                    # reads the vector-store key as last resolved in this
                    # process, so it is resolved once before a phase that
                    # may ask — a key set in Admin -> Settings counts from
                    # the next phase. A failure here costs the phase its
                    # knowledge base, never the run.
                    from app.services import app_settings_service

                    try:
                        await app_settings_service.get_secret_setting(
                            db, "kb.pinecone_api_key"
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "kb_key_refresh_failed", error_type=type(exc).__name__
                        )

                inp = AgentInput(
                    run_id=run.id,
                    tenant_id=run.tenant_id,
                    user_inputs=run.user_inputs or {},
                    prior_analysis=(
                        snap.analysis if (i > 0 or rerun) else None
                    ),
                    user_edits=(
                        user_edits if (rerun and i == start_index) else None
                    ),
                    capabilities=_capabilities.for_run(
                        run_id=run.id,
                        tenant_id=run.tenant_id,
                        # Whose configuration ``caps.config`` reads (K5a).
                        agent_id=manifest.id,
                        grants=manifest.capabilities,
                        user_id=run.user_id,
                        run_token=facade_token,
                        # K8a: an in-process agent's tool secret falls back
                        # to the backend's environment, the process it runs
                        # in (L20); a container's environment is its own.
                        secrets_env_fallback=manifest.runtime != "container",
                        # The SAME number ``asyncio.timeout`` below is
                        # about to wrap this phase in. A model call's
                        # transport ceiling comes from what is left of
                        # it, so the client can never hang up on a call
                        # the step's own timeout still allows.
                        deadline_seconds=deadline,
                    ),
                    deadline_seconds=deadline,
                    user_id=run.user_id,
                    run_number=run.run_number,
                )
                # One enforcement point for every runtime (blueprint S4):
                # the invocation is cancelled at the deadline and the run
                # fails with it named; the container runtime forwards the
                # same number to the agent and bounds its token with it.
                try:
                    async with asyncio.timeout(deadline):
                        result = await agent.run_phase(
                            spec.name, inp, _on_progress(redis, run.id)
                        )
                except TimeoutError as exc:
                    raise PhaseDeadlineExceeded(
                        f"phase {spec.name!r} exceeded its deadline of "
                        f"{deadline}s (manifest phases[].deadline_seconds, "
                        f"ceiling LIBRERUN_MAX_PHASE_SECONDS)"
                    ) from run_boundary.scrub_exception(exc)
                except Exception as exc:
                    # The phase span records what leaves this block and
                    # run_failed logs it, and neither walks it: a tool
                    # secret in the agent's exception is scrubbed first,
                    # its type kept for _classify (Codex on #173).
                    scrubbed = run_boundary.scrub_exception(exc)
                    if scrubbed is exc:
                        raise
                    raise scrubbed from None
                finally:
                    # The moment the invocation ends, so does the token's
                    # licence to spend — including on the deadline
                    # cancellation path, which is exactly when an agent
                    # that is still running would keep calling.
                    if facade_token and facade_record:
                        try:
                            await _run_token.end(redis, facade_token, facade_record)
                        except Exception:  # noqa: BLE001 — the TTL backs it up
                            pass

                structured = dict(getattr(result, "structured", None) or {})
                structured.pop("_drifts", None)
                ok = getattr(result, "status", None) in _SUCCESS_STATUSES
                report_html = getattr(result, "report_html", None) if is_final else None

                # Terminal output is agent-supplied and about to be
                # persisted (blueprint S4, gap H7): string leaves are
                # redacted, keys and numbers checked, and a flagged one
                # ends the run ``error`` with reason ``pii_in_output`` —
                # the path named, never the value, nothing stored. The
                # run's tool secrets are scrubbed first, re-resolved now
                # (K8a).
                await _scrub_run_secrets(run, manifest)
                try:
                    structured = run_boundary.walk_value(
                        structured, argument="output", reason=run_boundary.REASON_OUTPUT
                    )
                    report_html = run_boundary.redact_text(report_html)
                except (PiiRefused, UnwalkableValue) as exc:
                    reason = getattr(exc, "reason", "output_unwalkable")
                    finding = getattr(exc, "finding", None)
                    logger.error(
                        "run_output_refused",
                        reason=reason,
                        argument=getattr(exc, "argument", "output"),
                        path=getattr(finding, "path", None),
                        pii_type=getattr(finding, "pii_type", None),
                        kind=getattr(finding, "kind", None),
                    )
                    _set_error(
                        run,
                        run_errors.OUTPUT_REFUSED,
                        # The path and the class, never the value — the
                        # same rule the boundary's own log line follows.
                        f"{reason} at {getattr(finding, 'path', None)!r} "
                        f"({getattr(finding, 'pii_type', None)})",
                    )
                    await db.commit()
                    span.set_attribute("librerun.output.refused", reason)
                    logger.info(
                        "phase_completed", outcome="refused", run_status=run.status
                    )
                    return

                # What the agent said when it did not succeed: its
                # result's ``error`` text if it gave one, else the status
                # word it used. Operator-facing, hence ``error_detail``.
                agent_said = (
                    getattr(result, "error", None)
                    or f"phase {spec.name!r} ended with status "
                    f"{getattr(result, 'status', None)!r}"
                )
                if is_final:
                    snap.structured_data = structured
                    snap.report_html = report_html
                    if ok:
                        run.status = "complete"
                    else:
                        _set_error(run, run_errors.AGENT_FAILED, agent_said)
                else:
                    snap.analysis = structured
                    if not ok:
                        _set_error(run, run_errors.AGENT_FAILED, agent_said)
                    elif phases[i + 1].approval:
                        run.status = "awaiting_approval"
                    # else: leave the running status; the next loop turn
                    # flips it as it starts (ungated auto-advance).
                await db.commit()

                if is_final:
                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        safe_json(
                            {
                                "status": getattr(result, "status", None),
                                "structured_keys": sorted(list(structured.keys())),
                                "report_chars": len(
                                    getattr(result, "report_html", None) or ""
                                ),
                            }
                        ),
                    )
                else:
                    span.set_attribute(
                        SpanAttributes.OUTPUT_VALUE,
                        safe_json(
                            {
                                "status": getattr(result, "status", None),
                                "display": getattr(result, "display", None),
                            }
                        ),
                    )
                span.set_attribute(SpanAttributes.OUTPUT_MIME_TYPE, _JSON_MIME)
                logger.info(
                    "phase_completed",
                    outcome=getattr(result, "status", None),
                    run_status=run.status,
                )

                if not ok or run.status in ("awaiting_approval", "complete"):
                    return
        i += 1
