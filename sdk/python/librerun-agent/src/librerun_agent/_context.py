"""The invocation record and the ``RunContext`` an agent's handler sees."""
from __future__ import annotations

import asyncio
import contextvars
import datetime as _dt
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ._llm import Config, Llm
from ._mcp import Capabilities, MCPClient, Pii, Secrets

# The invocation the current task (or a thread it started) belongs to.
# Set by the server around the handler; read by the stdout/stderr writers
# and the exporters — a write with no invocation has no owner.
CURRENT: contextvars.ContextVar["Invocation | None"] = contextvars.ContextVar(
    "librerun_agent_invocation", default=None
)


@dataclass
class Invocation:
    """One execution of one phase (the Run Contract's ``invocation_id``)."""

    id: str
    token: str
    run_id: str
    tenant_id: str | None
    phase: str
    input: dict
    prior_output: dict | None
    user_edits: str | None
    rerun: bool
    deadline_seconds: int | None
    mcp_url: str | None
    traceparent: str | None
    tracestate: str | None
    trace_id: str | None
    started_at: float = field(default_factory=time.monotonic)
    status: str = "running"  # running | completed | failed
    output: dict | None = None
    error: str | None = None
    events: list[tuple[str, dict]] = field(default_factory=list)
    finished_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, repr=False)
    _wakeups: list[asyncio.Event] = field(default_factory=list, repr=False)

    # -- events (thread-safe: a print() from a worker thread lands here) --

    def emit(self, event: str, data: dict) -> None:
        with self._lock:
            self.events.append((event, data))
            wakeups = list(self._wakeups)
        loop = self._loop
        if loop is not None:
            for wakeup in wakeups:
                loop.call_soon_threadsafe(wakeup.set)

    def register_wakeup(self, wakeup: asyncio.Event) -> None:
        with self._lock:
            self._wakeups.append(wakeup)

    def unregister_wakeup(self, wakeup: asyncio.Event) -> None:
        with self._lock:
            if wakeup in self._wakeups:
                self._wakeups.remove(wakeup)

    def snapshot(self, since: int) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self.events[since:])

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "failed")

    @property
    def deadline_at(self) -> float | None:
        if self.deadline_seconds is None:
            return None
        return self.started_at + self.deadline_seconds

    def seconds_left(self) -> float | None:
        at = self.deadline_at
        return None if at is None else max(0.0, at - time.monotonic())


class RunContext:
    """What the handler is given (blueprint §9). Read-only facts about the
    invocation, ``progress()`` and ``log()`` for the Run Contract events,
    ``capabilities`` for the run-scoped MCP tools, ``pii.redact`` for the
    intake pipeline on demand, ``llm.complete`` for a model call through
    the gateway — or ``llm.client()`` and ``llm.step()`` when a framework
    makes the call for you — ``config.steps()`` for the effective step
    configuration this tenant's admin chose, and ``secrets.get(name)`` for
    one of the tool secrets the manifest declares, this tenant's value."""

    def __init__(self, invocation: Invocation):
        self._inv = invocation
        client = (
            MCPClient(invocation.mcp_url, invocation.token, traceparent=invocation.traceparent)
            if invocation.mcp_url
            else None
        )
        self.capabilities = Capabilities(client)
        self.pii = Pii(client)
        # Blueprint S4a: model calls go to the gateway with this
        # invocation's run token, and the step's configuration is read
        # live so an admin's edit takes effect on the next call.
        self.llm = Llm(invocation)
        self.config = Config(client)
        # K8b: the tool secrets agent.yaml declares (secrets[]), read over
        # MCP when asked for and kept nowhere.
        self.secrets = Secrets(client)

    # -- read-only facts ----------------------------------------------------

    @property
    def run_id(self) -> str:
        """The platform run this invocation belongs to."""
        return self._inv.run_id

    @property
    def invocation_id(self) -> str:
        return self._inv.id

    @property
    def phase(self) -> str:
        return self._inv.phase

    @property
    def input(self) -> dict:
        return self._inv.input

    @property
    def prior_output(self) -> dict | None:
        return self._inv.prior_output

    @property
    def user_edits(self) -> str | None:
        return self._inv.user_edits

    @property
    def rerun(self) -> bool:
        return self._inv.rerun

    @property
    def tenant_id(self) -> str | None:
        return self._inv.tenant_id

    @property
    def deadline(self) -> _dt.datetime | None:
        """When the chassis will fail this invocation (UTC), or None."""
        left = self._inv.seconds_left()
        if left is None:
            return None
        return _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=left)

    @property
    def seconds_left(self) -> float | None:
        return self._inv.seconds_left()

    @property
    def traceparent(self) -> str | None:
        """The chassis phase span's W3C context — the run's one trace."""
        return self._inv.traceparent

    @property
    def tracestate(self) -> str | None:
        return self._inv.tracestate

    @property
    def mcp_url(self) -> str | None:
        return self._inv.mcp_url

    # -- the Run Contract events ---------------------------------------------

    def progress(
        self,
        status: str,
        *,
        step: str,
        label: str | None = None,
        detail: str | None = None,
    ) -> None:
        """A ``progress`` event: ``status`` is ``running`` | ``completed`` |
        ``failed`` (the chassis maps it onto its own vocabulary)."""
        data: dict[str, Any] = {"step_id": step, "status": status, "detail": detail}
        if label is not None:
            data["label"] = label
        self._inv.emit("progress", data)

    def log(self, message: str, level: str = "info") -> None:
        """A ``log`` event: an operator-facing line the chassis re-emits
        into its own structured stream, redacted at ingestion."""
        self._inv.emit("log", {"level": level, "message": str(message)})


__all__ = ["CURRENT", "Invocation", "RunContext"]
