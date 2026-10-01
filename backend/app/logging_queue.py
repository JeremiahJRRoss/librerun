"""Queue-only logging (blueprint S4; gaps H9, H10): every record of both
planes is walked before any sink sees it, and no registration path can
attach a handler that bypasses the walk.

How a record travels:

1. **Emission** — ``logging.Logger.callHandlers`` is replaced at the
   class level by :func:`_dispatch`, which consults no logger's
   ``handlers`` list at all: it captures what only the emitting thread
   knows — the structlog contextvars (the plane, the run identity), the
   active span's ids, the ``exc_info`` tuple a structlog ``exception()``
   call left as ``True``, the stack a ``stack_info=True`` asked for —
   onto the record and puts it on the queue. A handler that reaches a
   list by any road (``addHandler``, a non-incremental ``dictConfig``,
   an assignment to ``logger.handlers``, a ``removeHandler`` of the
   marker) receives nothing and removes nothing, because dispatch never
   looks there. ``addHandler`` is still wrapped: inside an invocation it
   refuses anything but the marker with a ``RuntimeError`` and a
   platform-plane warning, the loud signal to the author; outside one —
   a library attaching a handler at import — it attaches nothing and
   warns, because a failed import there would cost the process the very
   libraries the redactor is built on.
2. **The listener thread** renders ``exc_info`` and ``stack_info`` to
   text on the record, walks it once (``walkers.walk_log_record``: the
   rendered message, the rendered traceback, every string field, every
   caller-supplied extra; the stdlib's own numbers never) and fans it
   out by logger name to the sinks — the JSONL writer, the stderr
   stream (what the container logging driver persists) and, under
   ``OTEL_DEBUG``, the debug sinks for ``opentelemetry.*``. A record
   the walk refuses is dropped for one platform-plane warning naming
   the logger and the count, never the text. Presidio's cost sits on
   this thread, off the request path.
3. **The sinks** write to the *original* descriptors: ``sys.stdout`` and
   ``sys.stderr`` are replaced with writers that capture the caller's
   context at each write and emit a record per line (a ``print()``
   inside an invocation is a run-plane record of that run), and, as the
   belt beneath them, file descriptors 1 and 2 are a pipe read by a
   thread that feeds each remaining line — a C library's ``fprintf``
   carries no context — to the queue on the platform plane.
4. **Threads** an invocation spawns inherit its context
   (``threading.Thread.start`` captures the caller's context and runs the
   target inside it) **except executor workers**, which serve many
   invocations and carry none: a ``concurrent.futures`` work item runs
   in the context captured at ``submit`` instead — the callable, the
   ``set_result``/``set_exception`` and the done callbacks that fire
   inside them — which also covers ``asyncio``'s default executor.

``install`` is the boot check too: every handler already attached to
any logger — uvicorn's default configuration on its own loggers, the
``NullHandler`` a library attaches at import, a ``StreamHandler`` spaCy
or an instrumentation package attaches, anything a test attached before
the patch — is **superseded**: removed and reported, one line naming the
logger and the handler class (a ``NullHandler`` at debug, since it
emits nothing by construction). Refusing to boot instead would make the
chassis hostage to what its imports do with ``logging`` before it can
patch. :func:`enforce` is the belt the runner re-runs before each
invocation for handlers attached after the patch by a road the guard
does not see.
"""
from __future__ import annotations

import atexit
import concurrent.futures.thread as _cf_thread
import contextvars
import io
import logging
import logging.handlers
import os
import queue
import sys
import threading
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import structlog

from app.observability import walkers

logger = structlog.get_logger(walkers.WALK_LOGGER)

_ORIGINAL_CALL_HANDLERS = logging.Logger.callHandlers
_ORIGINAL_ADD_HANDLER = logging.Logger.addHandler
_ORIGINAL_THREAD_START = threading.Thread.start
_ORIGINAL_WORK_ITEM_INIT = _cf_thread._WorkItem.__init__
_ORIGINAL_WORK_ITEM_RUN = _cf_thread._WorkItem.run

# Handler classes from these modules are neither strays nor refused —
# empty in production; the test suite names pytest's own capture
# handlers here so the real ``configure_logging`` can be driven under it.
DEFAULT_TOLERATED_MODULES: tuple[str, ...] = ()

_STOP = object()


class QueueOnlyViolation(RuntimeError):
    """Raised by :func:`check_strict` when a handler other than the queue
    marker is attached to a logger — the belt's loud form."""


class QueueMarker(logging.Handler):
    """The one handler visible on the root: a marker that says "queue
    only". Dispatch never calls it; it exists so ``root.handlers`` says
    what the pipeline is and the guard has something to allow."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover — never dispatched to
        return None


class GuardedFileHandler(logging.handlers.TimedRotatingFileHandler):
    """A sink a foreign ``dictConfig`` cannot close from under us:
    ``close()`` is honoured only from this module's own shutdown."""

    _allow_close = False

    def close(self) -> None:
        if self._allow_close:
            super().close()


class GuardedStreamHandler(logging.StreamHandler):
    _allow_close = False

    def close(self) -> None:
        if self._allow_close:
            super().close()


@dataclass
class _State:
    queue: "queue.Queue[Any]"
    marker: QueueMarker
    root_sinks: list[logging.Handler]
    debug_sinks: list[logging.Handler]
    debug_prefix: str
    tolerate_modules: tuple[str, ...]
    listener: threading.Thread | None = None
    redact: bool = True
    dropped: int = 0
    stdio: "_StdioState | None" = None
    context_installed: bool = False


_STATE: _State | None = None
_LOCK = threading.Lock()


def installed() -> bool:
    return _STATE is not None


# --------------------------------------------------------------------------
# Emission: capture and enqueue
# --------------------------------------------------------------------------


def _capture(record: logging.LogRecord) -> None:
    """What only the emitting thread knows, stamped on the record."""
    ctx = structlog.contextvars.get_contextvars()
    record._librerun_ctx = dict(ctx) if ctx else {}  # type: ignore[attr-defined]
    try:
        from opentelemetry import trace as _trace

        sc = _trace.get_current_span().get_span_context()
        record._librerun_otel = (  # type: ignore[attr-defined]
            (format(sc.trace_id, "032x"), format(sc.span_id, "016x")) if sc.is_valid else None
        )
    except Exception:  # noqa: BLE001 — logging must never raise for telemetry
        record._librerun_otel = None  # type: ignore[attr-defined]
    # A structlog ``exception()`` leaves ``exc_info=True`` for the
    # formatter to resolve from ``sys.exc_info()`` — on the listener
    # thread that is nothing, so it is resolved here, with the stack and
    # the positional args, on the thread that logged. One
    # implementation, in ``walkers``: the gateway has to do the same
    # three things, and an args-rendering that existed in one process
    # and not the other is an unwalked ``%s`` in the other.
    #
    # Three frames of ours to drop from a rendered stack: ``_capture``,
    # ``_dispatch`` and ``Logger.handle``.
    walkers.prepare_record(record, stack_skip=3)


def _dispatch(self: logging.Logger, record: logging.LogRecord) -> None:
    state = _STATE
    if state is None:
        _ORIGINAL_CALL_HANDLERS(self, record)
        return
    try:
        _capture(record)
        # A record the listener thread itself emits — the walk's own
        # warnings, a library's chatter from inside the redactor (Presidio
        # logs on every analyze call) — is never walked: walking it would
        # log again, and the pipeline would feed itself forever.
        if threading.current_thread() is state.listener:
            record._librerun_from_walk = True  # type: ignore[attr-defined]
        state.queue.put_nowait(record)
    except Exception:  # noqa: BLE001
        # Never raise into the caller; the record is lost, logging is not.
        pass


def _in_invocation() -> bool:
    """True while a task is executing an agent run — the runner binds
    ``agent_id`` around every phase (the run-plane predicate)."""
    try:
        return bool(structlog.contextvars.get_contextvars().get("agent_id"))
    except Exception:  # noqa: BLE001
        return False


def _guarded_add_handler(self: logging.Logger, handler: logging.Handler) -> None:
    """``addHandler`` under the queue-only pipeline. The marker and
    tolerated handlers attach; anything else attaches nothing — dispatch
    would ignore it anyway — and is reported. Inside an invocation the
    report is a ``RuntimeError``, the loud signal to an agent author;
    outside one (a library attaching its handler at import — spaCy does,
    and a failed import there would cost the redactor itself) it is a
    platform-plane warning, a ``NullHandler`` a debug line."""
    state = _STATE
    if state is None or handler is state.marker or _tolerated(handler, state):
        _ORIGINAL_ADD_HANDLER(self, handler)
        return
    if isinstance(handler, logging.NullHandler):
        logger.debug("logging_handler_ignored", logger_name=self.name, handler="NullHandler")
        return
    if _in_invocation():
        logger.warning(
            "logging_handler_refused",
            logger_name=self.name,
            handler=type(handler).__name__,
            reason="queue_only",
        )
        raise RuntimeError(
            "LibreRun logging is queue-only: every record is walked on the "
            "listener thread and fanned out to the configured sinks; a "
            f"{type(handler).__name__} attached to logger {self.name!r} "
            "would bypass the walk. Log through the standard library or "
            "structlog and let the chassis route it."
        )
    logger.warning(
        "logging_handler_ignored",
        logger_name=self.name,
        handler=type(handler).__name__,
        reason="queue_only",
    )


def _tolerated(handler: logging.Handler, state: _State) -> bool:
    module = type(handler).__module__ or ""
    return any(module == m or module.startswith(m + ".") for m in state.tolerate_modules)


# --------------------------------------------------------------------------
# The listener: render, walk, fan out
# --------------------------------------------------------------------------


def _render(record: logging.LogRecord) -> None:
    """Tracebacks and stacks to text, before the walk.

    One implementation, in ``walkers``, because the gateway walks at
    emission and must render first for the same reason this thread does.
    """
    walkers.render_exceptions(record)


def _fan_out(state: _State, record: logging.LogRecord) -> None:
    if state.debug_sinks and record.name.startswith(state.debug_prefix):
        sinks = state.debug_sinks
    else:
        sinks = state.root_sinks
    for sink in sinks:
        try:
            sink.handle(record)
        except Exception:  # noqa: BLE001
            pass


def _process(state: _State, record: logging.LogRecord) -> None:
    try:
        _render(record)
        if state.redact and not getattr(record, "_librerun_from_walk", False):
            walkers.walk_log_record(record)
    except walkers.RecordRefused as refused:
        state.dropped += 1
        logger.warning(
            "log_record_dropped",
            logger_name=record.name,
            count=1,
            kind=refused.kind,
            pii_type=refused.pii_type,
            path=refused.path,
        )
        return
    except Exception as exc:  # noqa: BLE001
        # Fail closed: a record the walk could not process is not shown.
        state.dropped += 1
        logger.warning(
            "log_record_dropped",
            logger_name=record.name,
            count=1,
            kind="walk_error",
            pii_type=type(exc).__name__,
        )
        return
    _fan_out(state, record)


def _listen(state: _State) -> None:
    while True:
        item = state.queue.get()
        if item is _STOP:
            state.queue.task_done()
            return
        try:
            _process(state, item)
        finally:
            state.queue.task_done()


# --------------------------------------------------------------------------
# Install / check / enforce / uninstall
# --------------------------------------------------------------------------


def _all_loggers() -> Iterable[logging.Logger]:
    yield logging.getLogger()
    for candidate in list(logging.Logger.manager.loggerDict.values()):
        if isinstance(candidate, logging.Logger):
            yield candidate


def check(*, tolerate_modules: tuple[str, ...] = ()) -> list[tuple[str, logging.Handler]]:
    """Every handler attached to any logger that is not the marker and
    not from a tolerated module — the boot check's finding list."""
    state = _STATE
    marker = state.marker if state is not None else None
    tolerated = tuple(tolerate_modules) + (state.tolerate_modules if state else ())
    found: list[tuple[str, logging.Handler]] = []
    for lg in _all_loggers():
        for h in list(lg.handlers):
            if h is marker:
                continue
            module = type(h).__module__ or ""
            if any(module == m or module.startswith(m + ".") for m in tolerated):
                continue
            found.append((lg.name, h))
    return found


def check_strict() -> None:
    """Raise :class:`QueueOnlyViolation` naming every stray handler."""
    strays = check()
    if strays:
        described = ", ".join(f"{n or 'root'}: {type(h).__name__}" for n, h in strays)
        raise QueueOnlyViolation(
            "a handler is attached that would bypass the walk if dispatch "
            f"consulted handler lists: {described}"
        )


def _remove_and_report(strays: list[tuple[str, logging.Handler]], *, event: str) -> None:
    for name, handler in strays:
        is_null = isinstance(handler, logging.NullHandler)
        (logger.debug if is_null else logger.warning)(
            event, logger_name=name or "root", handler=type(handler).__name__
        )
        try:
            logging.getLogger(name).handlers.remove(handler)
        except ValueError:
            pass


def enforce() -> int:
    """The belt: remove stray handlers (dispatch ignores them anyway) and
    say so. No-op when the pipeline is not installed."""
    if _STATE is None:
        return 0
    strays = check()
    _remove_and_report(strays, event="logging_stray_handler_removed")
    return len(strays)


def install(
    *,
    root_sinks: list[logging.Handler],
    debug_sinks: list[logging.Handler] | None = None,
    debug_prefix: str = "opentelemetry",
    redact: bool = True,
    tolerate_modules: tuple[str, ...] = (),
) -> list[tuple[str, logging.Handler]]:
    """Install the queue-only pipeline (idempotent: a previous install is
    torn down first). The boot check: every handler attached before the
    patch is superseded — removed and reported — and returned."""
    global _STATE
    with _LOCK:
        if _STATE is not None:
            _uninstall_locked()
        marker = QueueMarker()
        tolerated = tuple(tolerate_modules) + tuple(DEFAULT_TOLERATED_MODULES)
        state = _State(
            queue=queue.Queue(),
            marker=marker,
            root_sinks=list(root_sinks),
            debug_sinks=list(debug_sinks or []),
            debug_prefix=debug_prefix,
            tolerate_modules=tolerated,
            redact=redact,
        )
        superseded = [
            (name, h)
            for name, h in check(tolerate_modules=tolerated)
            if h is not marker
        ]
        root = logging.getLogger()
        _ORIGINAL_ADD_HANDLER(root, marker)
        logging.Logger.callHandlers = _dispatch  # type: ignore[method-assign]
        logging.Logger.addHandler = _guarded_add_handler  # type: ignore[method-assign]
        state.listener = threading.Thread(
            target=_listen, args=(state,), name="librerun-log-walk", daemon=True
        )
        _STATE = state
        state.listener.start()
        # Reported through the pipeline itself, once it is up.
        _remove_and_report(superseded, event="logging_handler_superseded")
        return superseded


def _uninstall_locked() -> None:
    global _STATE
    state = _STATE
    if state is None:
        return
    logging.Logger.callHandlers = _ORIGINAL_CALL_HANDLERS  # type: ignore[method-assign]
    logging.Logger.addHandler = _ORIGINAL_ADD_HANDLER  # type: ignore[method-assign]
    _STATE = None
    if state.listener is not None:
        state.queue.put(_STOP)
        state.listener.join(timeout=10)
    root = logging.getLogger()
    if state.marker in root.handlers:
        root.handlers.remove(state.marker)
    for sink in state.root_sinks + state.debug_sinks:
        try:
            sink.flush()
            sink._allow_close = True  # type: ignore[attr-defined]
            sink.close()
        except Exception:  # noqa: BLE001
            pass
    if state.stdio is not None:
        _restore_stdio(state.stdio)
    if state.context_installed:
        _uninstall_thread_context()


def uninstall() -> None:
    with _LOCK:
        _uninstall_locked()


def drain(timeout: float = 5.0) -> None:
    """Wait until every queued record has been processed (tests, shutdown)."""
    state = _STATE
    if state is None:
        return
    state.queue.join()
    for sink in state.root_sinks + state.debug_sinks:
        try:
            sink.flush()
        except Exception:  # noqa: BLE001
            pass


def dropped_count() -> int:
    return _STATE.dropped if _STATE is not None else 0


atexit.register(lambda: (drain(), uninstall()))


# --------------------------------------------------------------------------
# stdout / stderr capture and the descriptor belt
# --------------------------------------------------------------------------


@dataclass
class _StdioState:
    stdout: Any
    stderr: Any
    fd_backups: dict[int, int] = field(default_factory=dict)
    pipes: dict[int, tuple[int, int]] = field(default_factory=dict)
    readers: list[threading.Thread] = field(default_factory=list)


class CapturingWriter(io.TextIOBase):
    """A ``sys.stdout``/``sys.stderr`` replacement: each complete line is
    emitted as a log record from the caller's thread, so the dispatcher
    captures the caller's context — the plane and the run identity.

    The pending text is buffered PER CONTEXT, not once for the process.
    A single buffer joins fragments from concurrent runs: two agents
    writing without a newline interleave in it, and the thread that
    finally supplies one emits the join as its own run's line — one
    tenant's text inside another tenant's telemetry. The key is the
    bound run, so a run's partial writes can only ever meet its own.
    """

    def __init__(self, name: str, level: int, fallback: Any):
        super().__init__()
        self._name = name
        self._level = level
        self._fallback = fallback
        self._buffers: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _context_key() -> str:
        """Whose pending text this is: the bound run, else the thread.

        Outside a run there is no run to mix with, but two threads can
        still interleave, so the thread is the fallback owner.
        """
        context = structlog.contextvars.get_contextvars()
        run_id = context.get("run_id")
        if run_id:
            return f"run:{run_id}"
        return f"thread:{threading.get_ident()}"

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    def fileno(self) -> int:
        return self._fallback.fileno()

    def isatty(self) -> bool:
        return False

    def write(self, s: str) -> int:
        if not isinstance(s, str):
            s = str(s)
        key = self._context_key()
        with self._lock:
            buffered = self._buffers.get(key, "") + s
            lines = buffered.split("\n")
            self._buffers[key] = lines.pop()
            if not self._buffers[key]:
                del self._buffers[key]
        for line in lines:
            self._emit(line)
        return len(s)

    def flush(self) -> None:
        key = self._context_key()
        with self._lock:
            pending = self._buffers.pop(key, "")
        if pending:
            self._emit(pending)

    def flush_all(self) -> None:
        """Every context's pending text, for teardown."""
        with self._lock:
            pending, self._buffers = list(self._buffers.values()), {}
        for text in pending:
            if text:
                self._emit(text)

    def _emit(self, line: str) -> None:
        if not line.strip():
            return
        try:
            logging.getLogger(self._name).log(self._level, line)
        except Exception:  # noqa: BLE001
            try:
                self._fallback.write(line + "\n")
            except Exception:  # noqa: BLE001
                pass


def _read_pipe(fd: int, name: str, level: int) -> None:
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            line = line.rstrip("\n")
            if line.strip():
                try:
                    logging.getLogger(name).log(level, line)
                except Exception:  # noqa: BLE001
                    pass


def install_stdio_capture(*, fd_belt: bool = True) -> None:
    """Replace ``sys.stdout``/``sys.stderr`` with capturing writers and,
    with ``fd_belt``, route descriptors 1 and 2 through a pipe read by a
    thread. The sinks keep the original descriptors: call this AFTER
    :func:`install`, whose stderr sink was built on a duplicate."""
    state = _STATE
    if state is None or state.stdio is not None:
        return
    stdio = _StdioState(stdout=sys.stdout, stderr=sys.stderr)
    if fd_belt:
        for fd, name, level in ((1, "fd.stdout", logging.INFO), (2, "fd.stderr", logging.WARNING)):
            try:
                backup = os.dup(fd)
                r, w = os.pipe()
                os.dup2(w, fd)
                os.close(w)
            except OSError:
                continue
            stdio.fd_backups[fd] = backup
            stdio.pipes[fd] = (r, backup)
            reader = threading.Thread(
                target=_read_pipe, args=(r, name, level), name=f"librerun-{name}", daemon=True
            )
            reader.start()
            stdio.readers.append(reader)
    sys.stdout = CapturingWriter("stdout", logging.INFO, stdio.stdout)
    sys.stderr = CapturingWriter("stderr", logging.WARNING, stdio.stderr)
    state.stdio = stdio


def _restore_stdio(stdio: _StdioState) -> None:
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    sys.stdout = stdio.stdout
    sys.stderr = stdio.stderr
    for fd, backup in stdio.fd_backups.items():
        try:
            os.dup2(backup, fd)
            os.close(backup)
        except OSError:
            pass


def console_stream():
    """The operator's terminal, past the capture — or None.

    :func:`install_stdio_capture` replaces ``sys.stderr`` AND dup2s
    descriptor 2 onto a pipe, so both ordinary routes out of this
    process end in a log record — walked, queued, and written by the
    listener thread whenever it gets there. That is right for anything
    that might carry a value: it is how an agent's ``print()`` becomes
    run-plane telemetry rather than a leak on the host's stdout.

    It is wrong for the handful of messages that exist to be READ by the
    person watching the boot, carry no value by construction, and are
    useless late — the demo banner is the one. The first walk loads
    spaCy, so a queued banner can surface seconds after the API is
    already serving.

    Returns a stream on the descriptor taken before the belt. ``None``
    means there is nothing to prefer: no capture is installed (the test
    suite, and any deployment with ``LOG_QUEUE_ONLY`` off), so the
    caller's own ``sys.stderr`` already IS the console. A returned
    stream is the caller's to close when it is not ``_STATE.stdio``'s
    own — :func:`write_console` does that bookkeeping.
    """
    state = _STATE
    stdio = state.stdio if state is not None else None
    if stdio is None:
        return None
    backup = stdio.fd_backups.get(2)
    if backup is None:
        # sys.stderr was replaced but no fd belt was installed, so the
        # object saved then still writes to the real descriptor 2.
        return stdio.stderr
    try:
        return os.fdopen(
            os.dup(backup), "w", encoding="utf-8", errors="replace", buffering=1
        )
    except OSError:
        return None


def write_console(text: str) -> bool:
    """Write ``text`` to :func:`console_stream`, closing what it opened.

    True when it reached the console past the capture — which is what a
    caller asserts on. False means it did not, and the caller should
    write to its own ``sys.stderr``: correct either way, but only one of
    the two is immediate and verbatim, and exactly one of them happens.
    """
    stream = console_stream()
    if stream is None:
        return False
    state = _STATE
    stdio = state.stdio if state is not None else None
    opened = stdio is not None and stream is not stdio.stderr
    try:
        stream.write(text)
        stream.flush()
    except Exception:  # noqa: BLE001
        return False
    finally:
        if opened:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass
    return True


def sink_stream(fd: int):
    """A text stream on a duplicate of ``fd`` for a sink to write to —
    taken before the belt redirects the descriptor, so a sink never
    writes into its own pipe."""
    try:
        return os.fdopen(os.dup(fd), "w", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return sys.__stderr__ if fd == 2 else sys.__stdout__


# --------------------------------------------------------------------------
# Thread and executor context
# --------------------------------------------------------------------------


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
    # Constructed on the submitting thread, inside ``submit``.
    self._librerun_context = contextvars.copy_context()


def _work_item_run(self) -> None:
    ctx = getattr(self, "_librerun_context", None)
    if ctx is None:
        return _ORIGINAL_WORK_ITEM_RUN(self)
    return ctx.run(_ORIGINAL_WORK_ITEM_RUN, self)


def install_thread_context() -> None:
    """Threads inherit the starter's context; executor work items run in
    the submitter's; executor workers themselves carry none."""
    state = _STATE
    threading.Thread.start = _thread_start  # type: ignore[method-assign]
    _cf_thread._WorkItem.__init__ = _work_item_init  # type: ignore[method-assign]
    _cf_thread._WorkItem.run = _work_item_run  # type: ignore[method-assign]
    if state is not None:
        state.context_installed = True


def _uninstall_thread_context() -> None:
    threading.Thread.start = _ORIGINAL_THREAD_START  # type: ignore[method-assign]
    _cf_thread._WorkItem.__init__ = _ORIGINAL_WORK_ITEM_INIT  # type: ignore[method-assign]
    _cf_thread._WorkItem.run = _ORIGINAL_WORK_ITEM_RUN  # type: ignore[method-assign]


def uninstall_thread_context() -> None:
    _uninstall_thread_context()
    if _STATE is not None:
        _STATE.context_installed = False


__all__ = [
    "CapturingWriter",
    "GuardedFileHandler",
    "GuardedStreamHandler",
    "QueueMarker",
    "QueueOnlyViolation",
    "check",
    "check_strict",
    "drain",
    "dropped_count",
    "enforce",
    "install",
    "install_stdio_capture",
    "install_thread_context",
    "installed",
    "console_stream",
    "sink_stream",
    "write_console",
    "uninstall",
    "uninstall_thread_context",
]
