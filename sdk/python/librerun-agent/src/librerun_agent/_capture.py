"""stdout / stderr capture per invocation, and the context that follows
work into threads and executor jobs (blueprint S4).

``sys.stdout`` and ``sys.stderr`` become writers that read the current
invocation at each write and hand each complete line to it as a log
line under the invocation's trace — a ``print()`` from one of two
overlapping invocations lands under its own trace and never the
other's. A write with no invocation context — at startup, from a thread
no invocation started — has no owner and is dropped; so is a C-level
write to the raw descriptor, which carries no context (``docker logs``
on the container is empty by construction: ``logging: driver: none``).

An invocation's context follows into the threads it spawns:
``threading.Thread.start`` captures the caller's context and runs the
target inside it — except executor workers, which serve many
invocations and must carry none; instead the whole work item runs in
the submitter's context (the callable, ``set_result`` /
``set_exception`` and the done callbacks that fire inside them), which
also covers ``asyncio``'s default executor.
"""
from __future__ import annotations

import concurrent.futures.thread as _cf_thread
import contextvars
import io
import logging
import sys
import threading
from typing import Any, Callable

from ._context import CURRENT, Invocation

_ORIGINAL_THREAD_START = threading.Thread.start
_ORIGINAL_WORK_ITEM_INIT = _cf_thread._WorkItem.__init__
_ORIGINAL_WORK_ITEM_RUN = _cf_thread._WorkItem.run

# How a captured line reaches the platform: set by ``_otel`` when export is
# configured (an OTLP log record under the invocation's trace); the Run
# Contract ``log`` event otherwise.
_LINE_SINK: Callable[[Invocation, str, str, str], None] | None = None


def set_line_sink(sink: Callable[[Invocation, str, str, str], None] | None) -> None:
    global _LINE_SINK
    _LINE_SINK = sink


def deliver_line(invocation: Invocation, stream: str, level: str, line: str) -> None:
    sink = _LINE_SINK
    if sink is not None:
        sink(invocation, stream, level, line)
    else:
        invocation.emit("log", {"level": level, "message": line, "stream": stream})


class CapturingWriter(io.TextIOBase):
    def __init__(self, stream: str, level: str, original: Any):
        super().__init__()
        self._stream = stream
        self._level = level
        self._original = original
        self._buffers: dict[str, str] = {}
        self._lock = threading.Lock()

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._original.fileno()

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        invocation = CURRENT.get()
        if invocation is None:
            return len(s)  # no owner: dropped
        with self._lock:
            buffered = self._buffers.get(invocation.id, "") + s
            lines = buffered.split("\n")
            self._buffers[invocation.id] = lines.pop()
        for line in lines:
            if line.strip():
                deliver_line(invocation, self._stream, self._level, line)
        return len(s)

    def flush(self) -> None:
        invocation = CURRENT.get()
        if invocation is None:
            return
        with self._lock:
            pending = self._buffers.pop(invocation.id, "")
        if pending.strip():
            deliver_line(invocation, self._stream, self._level, pending)

    def flush_invocation(self, invocation: Invocation) -> None:
        with self._lock:
            pending = self._buffers.pop(invocation.id, "")
        if pending.strip():
            deliver_line(invocation, self._stream, self._level, pending)


_ORIGINALS: dict[str, Any] = {}


def install_stdio_capture() -> None:
    if _ORIGINALS:
        return
    _ORIGINALS["stdout"] = sys.stdout
    _ORIGINALS["stderr"] = sys.stderr
    sys.stdout = CapturingWriter("stdout", "info", _ORIGINALS["stdout"])
    sys.stderr = CapturingWriter("stderr", "warning", _ORIGINALS["stderr"])


def uninstall_stdio_capture() -> None:
    if not _ORIGINALS:
        return
    sys.stdout = _ORIGINALS.pop("stdout")
    sys.stderr = _ORIGINALS.pop("stderr")


def flush_invocation_output(invocation: Invocation) -> None:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, CapturingWriter):
            stream.flush_invocation(invocation)


def capture_installed() -> bool:
    return bool(_ORIGINALS)


# -- thread and executor context -------------------------------------------


def _is_executor_worker(thread: threading.Thread) -> bool:
    return getattr(thread, "_target", None) is _cf_thread._worker


def _thread_start(self: threading.Thread) -> None:
    if not _is_executor_worker(self) and not getattr(self, "_librerun_context_bound", False):
        ctx = contextvars.copy_context()
        original_run = self.run

        def run_in_context() -> None:
            ctx.run(original_run)

        self.run = run_in_context  # type: ignore[method-assign]
        self._librerun_context_bound = True  # type: ignore[attr-defined]
    _ORIGINAL_THREAD_START(self)


def _work_item_init(self, *args, **kwargs) -> None:
    _ORIGINAL_WORK_ITEM_INIT(self, *args, **kwargs)
    self._librerun_context = contextvars.copy_context()  # captured at submit


def _work_item_run(self) -> None:
    ctx = getattr(self, "_librerun_context", None)
    if ctx is None:
        return _ORIGINAL_WORK_ITEM_RUN(self)
    return ctx.run(_ORIGINAL_WORK_ITEM_RUN, self)


_CONTEXT_INSTALLED = False


def install_thread_context() -> None:
    global _CONTEXT_INSTALLED
    threading.Thread.start = _thread_start  # type: ignore[method-assign]
    _cf_thread._WorkItem.__init__ = _work_item_init  # type: ignore[method-assign]
    _cf_thread._WorkItem.run = _work_item_run  # type: ignore[method-assign]
    _CONTEXT_INSTALLED = True


def uninstall_thread_context() -> None:
    global _CONTEXT_INSTALLED
    threading.Thread.start = _ORIGINAL_THREAD_START  # type: ignore[method-assign]
    _cf_thread._WorkItem.__init__ = _ORIGINAL_WORK_ITEM_INIT  # type: ignore[method-assign]
    _cf_thread._WorkItem.run = _ORIGINAL_WORK_ITEM_RUN  # type: ignore[method-assign]
    _CONTEXT_INSTALLED = False


def thread_context_installed() -> bool:
    return _CONTEXT_INSTALLED


class InvocationLogHandler(logging.Handler):
    """Routes ``logging`` records emitted inside an invocation to it as
    log lines (the Run Contract ``log`` event, or an OTLP record when
    export is configured); records with no invocation are dropped."""

    def emit(self, record: logging.LogRecord) -> None:
        invocation = CURRENT.get()
        if invocation is None:
            return
        if record.name.startswith("librerun_agent"):
            # The SDK's own records — a captured line the OTLP sink routed
            # through ``logging`` — never come back through this handler.
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        if record.exc_info:
            import traceback

            message += "\n" + "".join(traceback.format_exception(*record.exc_info))
        level = record.levelname.lower()
        deliver_line(invocation, f"logging:{record.name}", level, message)


__all__ = [
    "CapturingWriter",
    "InvocationLogHandler",
    "capture_installed",
    "deliver_line",
    "flush_invocation_output",
    "install_stdio_capture",
    "install_thread_context",
    "set_line_sink",
    "thread_context_installed",
    "uninstall_stdio_capture",
    "uninstall_thread_context",
]
