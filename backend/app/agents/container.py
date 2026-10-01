"""Chassis-side Run Contract v1 client (blueprint B12a, L11(b)).

``ContainerAgent`` is the registry entry for a ``runtime: container``
agent: an ``AgentProtocol`` implementation that, instead of dispatching
phases to methods, drives the four-endpoint HTTP+SSE contract documented
in ``docs/authoring/Run_Contract_v1.md`` against the container named by the
manifest's ``container.url``.

The generic run lifecycle is untouched: ``agent_runner`` calls
``run_phase`` exactly as for in-process agents, so phase spans, approval
gates, snapshots and progress all work identically. One contract run =
one phase invocation; the container never sees the gate.

The chassis mints a per-run bearer token, sends it on every request for
that run, and expects the agent to reject other tokens — see the
contract doc's auth section. Every request of an invocation also carries
the W3C ``traceparent`` / ``tracestate`` of the chassis phase span —
itself a child of the run's root span (blueprint S4, §8) — so an agent
that adopts them joins the run's one trace; one that ignores them still
conforms.
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx
import structlog

from app.agents.manifest import AgentManifest
from app.agents.protocol import (
    AgentInput,
    AgentProtocol,
    AnalysisResult,
    InvestigationResult,
    StepProgress,
)
from app.observability import run_trace
from app.services import pii_service, run_boundary, run_token

logger = structlog.get_logger(__name__)

# Generous ceilings: agent phases legitimately run for minutes. The
# connect timeout is what protects operators from a dead URL — the
# healthz probe uses an even shorter one to fail fast with a clear
# message before any run state is created.
_HEALTHZ_TIMEOUT = httpx.Timeout(5.0)
_CONNECT_TIMEOUT_SECONDS = 10.0


def _run_timeout(deadline: int) -> httpx.Timeout:
    """The transport ceiling for one invocation, from ITS deadline.

    This was the constant `httpx.Timeout(600.0)` while a phase's deadline
    is configurable up to `LIBRERUN_MAX_PHASE_SECONDS` (3600), so an
    agent that emitted a progress event and then worked silently for
    longer than ten minutes — legal, inside a deadline that allows it —
    died at the transport instead of running to its deadline, and the
    run named a read timeout rather than the phase. The comment here
    used to say a container emitting progress never trips it, which is
    true only of agents that emit progress oftener than the constant.

    `agent_runner.py` already states the rule for model calls: "a model
    call's transport ceiling comes from what is left of it, so the client
    can never hang up on a call the step's own timeout still allows."
    The container client is the one place that had not adopted it. The
    runner's `asyncio.timeout(deadline)` starts before the POST and is
    still the single enforcement point — this ceiling can only fire after
    it, so the failure an operator sees is the deadline, named.
    """
    return httpx.Timeout(float(deadline), connect=_CONNECT_TIMEOUT_SECONDS)

_TERMINAL_EVENTS = frozenset({"completed", "failed"})

# Bounds on agent-supplied progress strings before they reach Redis and,
# on read, the progress response schema.
_MAX_STEP_ID_CHARS = 120
_MAX_DETAIL_CHARS = 500

# The wall-clock bound for one phase invocation is the runner's
# (blueprint S4): the manifest phase's ``deadline_seconds`` under
# ``LIBRERUN_MAX_PHASE_SECONDS``, resolved into ``AgentInput`` and
# enforced around ``run_phase`` — ``_run_timeout`` above derives the
# per-READ timeout from that same number, so the transport cannot cut a
# phase short of the deadline it was given. The same number
# goes to the agent in the POST body and bounds its run token: a token
# that outlives its invocation is a live tenant-scoped credential, so it
# expires this long after the deadline at most (revocation at phase end
# is best-effort).
_RUN_TOKEN_GRACE_SECONDS = run_token.GRACE_SECONDS
# How long an ``ended`` token record lives after its invocation (blueprint
# S4): the OTLP relay alone honours it, so a late batch — the SDK flushes
# before it answers ``completed``, but a flush can straddle the answer —
# still lands, and nothing else does: the MCP server and the gateway
# accept ``active`` records only.
RUN_TOKEN_END_GRACE_SECONDS = run_token.END_GRACE_SECONDS

# Wire → chassis progress vocabulary. The contract's statuses are
# running|completed|failed; the chassis progress schema
# (app/schemas/run.py StepProgress) speaks
# The wire vocabulary maps onto the five the platform stores. One table,
# in run_boundary, because every write goes through progress_write and a
# second copy here could disagree with it.
_PROGRESS_STATUS_MAP = run_boundary.WIRE_STATUS_MAP


class ContainerAgentError(RuntimeError):
    """A contract violation or transport failure talking to the agent
    container. The runner catches it like any agent exception: the run is
    marked errored and the message lands in the run-failure log."""


class ContainerAgent(AgentProtocol):
    """Registry proxy for one container agent."""

    def __init__(self, manifest: AgentManifest, base_url: str, agent_dir: Path):
        self.agent_id = manifest.id
        self.display_name = manifest.name
        self.description = manifest.description
        self._manifest = manifest
        self._base_url = base_url.rstrip("/")
        self._schema_path = agent_dir / (manifest.input_schema or "")

    # -- intake ------------------------------------------------------------

    def input_schema(self) -> dict:
        """The manifest-declared JSON Schema file, verified to exist at
        registration time."""
        with open(self._schema_path, encoding="utf-8") as f:
            return json.load(f)

    # -- phase execution ---------------------------------------------------

    async def run_phase(
        self,
        phase_name: str,
        inp: AgentInput,
        on_progress: Callable[[StepProgress], Awaitable[None]],
    ):
        """Drive one Run Contract v1 invocation for ``phase_name``."""
        token = secrets.token_urlsafe(32)
        # The phase span is current here (the runner opened it around
        # this call), so the injected pair is the phase's: the run's
        # trace id, the phase span as parent, the root's vendor state.
        trace_headers = run_trace.propagation_headers()
        headers = {"Authorization": f"Bearer {token}", **trace_headers}
        deadline = self._deadline_seconds(inp)
        await self._register_run_token(token, inp, trace_headers, deadline)

        try:
            return await self._run_phase_inner(
                phase_name, inp, on_progress, token, headers, deadline
            )
        finally:
            # Runs on the runner's deadline cancellation too: the record
            # is marked ended the moment the invocation completes or fails.
            await self._end_run_token(token, inp, trace_headers, deadline)

    @staticmethod
    def _deadline_seconds(inp: AgentInput) -> int:
        """The invocation's budget as the runner resolved it; a direct
        caller that resolved none gets the platform ceiling."""
        from app.config import settings

        if inp.deadline_seconds is not None:
            return int(inp.deadline_seconds)
        return int(settings.LIBRERUN_MAX_PHASE_SECONDS)

    @staticmethod
    def _token_ttl_seconds(deadline: int) -> int:
        return int(deadline) + _RUN_TOKEN_GRACE_SECONDS

    def _run_token_record(
        self, inp: AgentInput, trace_headers: dict, deadline: int | None = None
    ) -> dict:
        """What the chassis knows about the invocation a token authorizes.

        Built by ``app.services.run_token`` rather than here, because
        three readers — the MCP server, the OTLP relay and, from S4a, the
        LLM gateway — each check something different against this
        document, and a shape written in two places is how a field
        quietly stops being written in one of them.
        """
        return run_token.build_record(
            run_id=inp.run_id,
            tenant_id=inp.tenant_id,
            agent_id=self.agent_id,
            grants=list(self._manifest.capabilities),
            user_id=inp.user_id,
            run_number=inp.run_number,
            deadline_seconds=(
                deadline if deadline is not None else self._deadline_seconds(inp)
            ),
            trace_id=run_trace.current_trace_id(),
            traceparent=trace_headers.get(run_trace.TRACEPARENT_HEADER),
        )

    async def _register_run_token(
        self,
        token: str,
        inp: AgentInput,
        trace_headers: dict | None = None,
        deadline: int | None = None,
    ) -> None:
        """Bind the minted bearer to this run so the chassis MCP server
        (blueprint B13) can scope tool calls to it. Expires with the
        phase and is revoked when the phase ends. Uses a per-call client
        (two ops per phase) so the module-global pool's event-loop
        affinity never bites background tasks."""
        import redis.asyncio as aioredis

        from app.config import settings

        async with aioredis.from_url(
            settings.REDIS_URL.get_secret_value(), decode_responses=True
        ) as redis:
            resolved = deadline if deadline is not None else self._deadline_seconds(inp)
            await redis.set(
                f"run_token:{token}",
                json.dumps(self._run_token_record(inp, trace_headers or {}, resolved)),
                # Scoped to this invocation's own deadline rather than an
                # arbitrary window: a token that outlives its run is a
                # live tenant-scoped credential, and revocation below is
                # best-effort (it can be lost to a crash or a Redis blip).
                ex=self._token_ttl_seconds(resolved),
            )

    async def _end_run_token(
        self,
        token: str,
        inp: AgentInput,
        trace_headers: dict | None = None,
        deadline: int | None = None,
    ) -> None:
        """Mark the token ``ended``: the record lives on for
        ``RUN_TOKEN_END_GRACE_SECONDS`` so the relay can land a late
        batch, while the MCP server and the gateway refuse it from this
        moment. Best-effort — the deadline TTL backs it up."""
        import redis.asyncio as aioredis

        from app.config import settings

        try:
            record = self._run_token_record(inp, trace_headers or {}, deadline)
            record["state"] = "ended"
            async with aioredis.from_url(
                settings.REDIS_URL.get_secret_value(), decode_responses=True
            ) as redis:
                await redis.set(
                    f"run_token:{token}", json.dumps(record), ex=RUN_TOKEN_END_GRACE_SECONDS
                )
        except Exception:  # noqa: BLE001 — best-effort; the TTL backs it up
            pass

    async def _revoke_run_token(self, token: str) -> None:
        """Delete the record outright (kept for callers that must end a
        token with no grace at all)."""
        import redis.asyncio as aioredis

        from app.config import settings

        try:
            async with aioredis.from_url(
                settings.REDIS_URL.get_secret_value(), decode_responses=True
            ) as redis:
                await redis.delete(f"run_token:{token}")
        except Exception:  # noqa: BLE001 — revocation is best-effort; TTL backs it up
            pass

    async def _run_phase_inner(
        self,
        phase_name: str,
        inp: AgentInput,
        on_progress: Callable[[StepProgress], Awaitable[None]],
        token: str,
        headers: dict,
        deadline: int,
    ):
        from app.config import settings

        async with httpx.AsyncClient(timeout=_run_timeout(deadline)) as client:
            await self._probe_healthz(client)

            body = {
                "contract": "v1",
                "agent_id": self.agent_id,
                "phase": phase_name,
                # The invocation's wall-clock budget (blueprint S4, §8):
                # the runner fails the phase when it passes, so an agent
                # that watches it can stop cleanly rather than be cut off.
                "deadline_seconds": deadline,
                "run": {
                    # The chassis run id (blueprint S1, L18). ``case_id`` is
                    # the pre-S1 spelling, sent alongside for one release
                    # and dropped at v1.1 (docs/authoring/Run_Contract_v1.md).
                    "id": str(inp.run_id),
                    "case_id": str(inp.run_id),
                    "tenant_id": str(inp.tenant_id),
                    "rerun": inp.user_edits is not None,
                    # B13: where this chassis serves run-scoped MCP tools
                    # (kb/run_store/audit), authenticated by the same
                    # bearer this request carries.
                    "mcp": {
                        "url": f"{settings.LIBRERUN_PUBLIC_URL.rstrip('/')}/api/v1/mcp"
                    },
                },
                "input": inp.user_inputs or {},
                "prior_output": inp.prior_analysis,
                "user_edits": inp.user_edits,
            }
            try:
                resp = await client.post(
                    f"{self._base_url}/v1/runs", json=body, headers=headers
                )
            except httpx.HTTPError as exc:
                raise ContainerAgentError(
                    f"POST /v1/runs to {self._base_url} failed: {exc}"
                ) from exc
            if resp.status_code not in (200, 201):
                raise ContainerAgentError(
                    f"POST /v1/runs returned {resp.status_code}: {resp.text[:500]}"
                )
            # The agent's handle for this one execution of one phase is an
            # *invocation* id (blueprint S1, L18). ``run_id`` is the
            # pre-S1 spelling of the same field, accepted for one release
            # and dropped at v1.1 (docs/authoring/Run_Contract_v1.md).
            try:
                started = resp.json()
                invocation_id = started["invocation_id"] if "invocation_id" in started else started["run_id"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ContainerAgentError(
                    f"POST /v1/runs response is not {{'invocation_id': …}}: "
                    f"{resp.text[:500]}"
                ) from exc
            if not isinstance(invocation_id, str) or not invocation_id:
                raise ContainerAgentError(
                    f"invocation_id must be a non-empty string, got {invocation_id!r}"
                )
            run_id = invocation_id
            # The id is an opaque string — treat it as exactly one path
            # segment so reserved characters (/, ?, #, …) can't change
            # which endpoint the follow-up requests address.
            run_ref = quote(run_id, safe="")

            output = await self._consume_events(
                client, run_ref, headers, on_progress
            )
            if output is None:
                output = await self._fetch_output(client, run_ref, headers)

        return self._to_result(phase_name, output)

    # -- contract steps ----------------------------------------------------

    async def _probe_healthz(self, client: httpx.AsyncClient) -> None:
        try:
            resp = await client.get(
                f"{self._base_url}/healthz", timeout=_HEALTHZ_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise ContainerAgentError(
                f"agent container unreachable at {self._base_url} "
                f"(healthz: {exc}) — is the container running and the "
                f"manifest container.url correct?"
            ) from exc
        if resp.status_code != 200:
            raise ContainerAgentError(
                f"healthz at {self._base_url} returned {resp.status_code}"
            )

    async def _consume_events(
        self,
        client: httpx.AsyncClient,
        run_ref: str,
        headers: dict,
        on_progress: Callable[[StepProgress], Awaitable[None]],
    ) -> dict | None:
        """Stream the SSE feed to its terminal event.

        ``run_ref`` is the percent-encoded run id (one path segment).
        Returns the ``completed`` output payload (``None`` if the event
        omitted it — the caller falls back to the output endpoint) and
        raises ``ContainerAgentError`` on ``failed``, transport errors, or
        a stream that ends without a terminal event.
        """
        url = f"{self._base_url}/v1/runs/{run_ref}/events"
        try:
            async with client.stream(
                "GET",
                url,
                headers={**headers, "Accept": "text/event-stream"},
            ) as resp:
                if resp.status_code != 200:
                    raise ContainerAgentError(
                        f"events stream returned {resp.status_code}"
                    )
                event_name = ""
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())
                    elif line == "":
                        # Frame boundary.
                        if not event_name and not data_lines:
                            continue
                        payload = self._parse_data(event_name, data_lines)
                        terminal = await self._handle_event(
                            event_name, payload, on_progress
                        )
                        if terminal is not None:
                            return terminal.get("output")
                        event_name = ""
                        data_lines = []
                    # Comment lines (":" prefix) and unknown fields are
                    # ignored per the SSE spec.

                # A well-behaved agent ends its terminal frame with a
                # blank line, but closing the socket right after the
                # data line is legal too — flush what's pending rather
                # than failing a run that actually succeeded.
                if event_name or data_lines:
                    payload = self._parse_data(event_name, data_lines)
                    terminal = await self._handle_event(
                        event_name, payload, on_progress
                    )
                    if terminal is not None:
                        return terminal.get("output")
        except httpx.HTTPError as exc:
            raise ContainerAgentError(
                f"events stream for run {run_ref} failed: {exc}"
            ) from exc
        raise ContainerAgentError(
            f"events stream for run {run_ref} ended without a terminal "
            f"completed/failed event"
        )

    def _parse_data(self, event_name: str, data_lines: list[str]) -> dict:
        raw = "\n".join(data_lines).strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ContainerAgentError(
                f"event {event_name!r} carried non-JSON data: {raw[:200]}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ContainerAgentError(
                f"event {event_name!r} data must be a JSON object"
            )
        return parsed

    async def _handle_event(
        self,
        event_name: str,
        payload: dict,
        on_progress: Callable[[StepProgress], Awaitable[None]],
    ) -> dict | None:
        """Apply one event; return the payload if it was terminal.

        Every free-text field an agent emits through the contract —
        ``log.message``, ``progress.detail``, ``failed.error`` — is
        redacted here before it is logged, stored or forwarded (blueprint
        S4): intake was never the only door PII could enter by. A
        ``progress.step_id`` is an identifier (the field name of the
        progress hash): checked, never rewritten, and a flagged one drops
        the event with a warning naming the position — the run continues,
        because a progress event has no reply channel.
        """
        if event_name == "progress":
            wire_status = str(payload.get("status") or "running")
            step_id = str(payload.get("step_id") or "progress")[:_MAX_STEP_ID_CHARS]
            finding = pii_service.check_identifier(step_id, path="progress.step_id")
            if finding is not None:
                logger.warning(
                    "container_progress_dropped",
                    agent_id=self.agent_id,
                    position="progress.step_id",
                    reason=run_boundary.REASON_PROGRESS,
                    pii_type=finding.pii_type,
                )
                return None
            # Every field is agent-supplied, so every field is coerced to
            # what the progress schema accepts: a non-string ``detail``
            # (or a novel status) would be persisted verbatim and then
            # permanently 500 the run's progress endpoint on read.
            raw_detail = payload.get("detail")
            if raw_detail is None:
                detail = None
            elif isinstance(raw_detail, str):
                detail = raw_detail[:_MAX_DETAIL_CHARS]
            else:
                detail = json.dumps(raw_detail)[:_MAX_DETAIL_CHARS]
            # Blueprint S4c: when the detector is not ready the boundary
            # refuses the detail rather than forwarding text it could
            # not walk. A progress event has no reply channel, so the
            # rule is the flagged-step_id rule one block up — drop the
            # event, name the position, let the run continue.
            try:
                detail = run_boundary.redact_text(
                    detail,
                    argument="progress.detail",
                    reason=run_boundary.REASON_PROGRESS,
                )
            except run_boundary.PiiRefused as exc:
                logger.warning(
                    "container_progress_dropped",
                    agent_id=self.agent_id,
                    position="progress.detail",
                    reason=exc.reason,
                    pii_type=exc.finding.pii_type,
                )
                return None
            await on_progress(
                StepProgress(
                    step_id=step_id,
                    status=_PROGRESS_STATUS_MAP.get(wire_status, "running"),
                    detail=detail,
                )
            )
            return None
        if event_name == "log":
            # Same rule, same reason: a log line the chassis could not
            # walk is dropped with its position named, never logged as
            # it arrived.
            try:
                message = run_boundary.redact_text(
                    str(payload.get("message", ""))[:2000],
                    argument="log.message",
                    reason=run_boundary.REASON_PROGRESS,
                )
            except run_boundary.PiiRefused as exc:
                logger.warning(
                    "container_agent_log_dropped",
                    agent_id=self.agent_id,
                    position="log.message",
                    reason=exc.reason,
                    pii_type=exc.finding.pii_type,
                )
                return None
            logger.info(
                "container_agent_log",
                agent_id=self.agent_id,
                level=str(payload.get("level", "info"))[:20],
                message=message,
            )
            return None
        if event_name == "failed":
            # The run is ending either way; the only question is whether
            # the agent's own error text may be carried into the run's
            # error field. Unwalked, it may not — the reason replaces it.
            try:
                error = run_boundary.redact_text(
                    str(payload.get("error", "unknown"))[:2000],
                    argument="failed.error",
                    reason=run_boundary.REASON_OUTPUT,
                )
            except run_boundary.PiiRefused as exc:
                error = f"<withheld: {exc.reason}>"
            raise ContainerAgentError(f"agent reported failure: {error}")
        if event_name == "completed":
            return payload
        # Unknown events are ignored (forward compatibility, per contract).
        logger.debug(
            "container_agent_event_ignored",
            agent_id=self.agent_id,
            event=event_name,
        )
        return None

    async def _fetch_output(
        self, client: httpx.AsyncClient, run_ref: str, headers: dict
    ) -> dict:
        """Contract fallback when ``completed`` omitted the output."""
        try:
            resp = await client.get(
                f"{self._base_url}/v1/runs/{run_ref}/output", headers=headers
            )
        except httpx.HTTPError as exc:
            raise ContainerAgentError(
                f"output fetch for run {run_ref} failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise ContainerAgentError(
                f"output fetch for run {run_ref} returned {resp.status_code}"
            )
        try:
            output = resp.json().get("output")
        except ValueError as exc:
            raise ContainerAgentError(
                f"output endpoint returned non-JSON for run {run_ref}"
            ) from exc
        if not isinstance(output, dict):
            raise ContainerAgentError(
                f"output for run {run_ref} must be a JSON object"
            )
        return output

    # -- result mapping ----------------------------------------------------

    def _to_result(self, phase_name: str, output: Any):
        """Map the contract output onto the runner's result vocabulary.

        The runner persists ``structured`` (analysis for non-final phases,
        structured_data for the final one) and reads ``status`` against
        its success set — ``awaiting_approval`` from non-final phases,
        ``complete`` from the final one, exactly like in-process agents.
        ``output.report_html`` is the contract's one reserved key, for
        ``html_report``-mode containers.
        """
        if not isinstance(output, dict):
            raise ContainerAgentError(
                f"phase {phase_name!r} produced no usable output object"
            )
        if self._manifest.is_final(phase_name):
            return InvestigationResult(
                status="complete",
                structured=output,
                report_html=output.get("report_html"),
            )
        return AnalysisResult(display=output, structured=output)
