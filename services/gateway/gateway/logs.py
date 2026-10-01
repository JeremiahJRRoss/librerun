"""The gateway's logging pipeline: every record walked, exactly once,
before any handler sees it (blueprint S4a; gap H10 in this process).

Two things were wrong with the first version, and they compounded.

**A filter on the root logger is not a pipeline.** ``Logger.handle``
consults the filters of the logger the record was *emitted* on and no
other; propagation then walks up the hierarchy inside ``callHandlers``
and reaches ancestors' **handlers**, never their filters. So a filter
added to the root ran for ``logging.getLogger().info(...)`` — which
nothing calls — and for nothing else. Every line from ``gateway.egress``,
``uvicorn.error``, ``LiteLLM`` and every dependency went to stderr
unwalked.

**And the gateway's own lines never entered stdlib at all.** ``structlog``
with no ``configure()`` uses ``PrintLoggerFactory``: ``logger.info(...)``
formats and writes to stdout itself. No stdlib record, no handler, no
filter — nothing for a log walk of any shape to catch.

So: the walk moves to ``logging.Logger.callHandlers``, patched at the
class level, which is the one point every record passes exactly once
whatever logger emitted it and whatever ``propagate`` says; and structlog
is configured onto the stdlib factory with the chassis's own processor
chain, so a gateway line and a uvicorn line come out in one schema and
both arrive as records.

The walk runs **inline**, on the thread that logged — the backend moves
Presidio's cost to a listener thread, and this process cannot: it has no
queue and, more to the point, it already runs Presidio inline over every
request body it forwards. Pinning the noisy loggers below is what keeps
that honest; unpinned, Presidio's own INFO line per ``analyze`` call
would arrive as a record to be walked, by an ``analyze`` call.

Failure is closed, as it is in the backend: a record the walk refuses,
and a record the walk could not process, is dropped for one warning that
names the logger and the reason and never the text.
"""
from __future__ import annotations

import logging
import sys
import threading

import structlog

from app.logging_formatters import (
    SHARED_PROCESSORS,
    STDLIB_PRE_CHAIN,
    strip_positional_args,
)
from app.logging_pii import redact_user_content_processor
from app.observability import walkers

from gateway.config import settings

# Captured at import, before anything replaces it, so ``uninstall_walk``
# restores the stdlib's own function rather than whatever was installed
# on top of it.
_ORIGINAL_CALL_HANDLERS = logging.Logger.callHandlers

_walk_installed = False
_configured = False

# Levels, both for noise and for correctness. ``presidio-*`` is the
# load-bearing one: it logs an INFO line on every analyze call, which on
# a pipeline that analyses every log line is a line per line. ``httpx``
# and ``httpcore`` log a line per provider request carrying the URL.
_LOGGER_LEVELS = {
    "httpx": "WARNING",
    "httpcore": "WARNING",
    "presidio-analyzer": "WARNING",
    "presidio-anonymizer": "WARNING",
}

# Uvicorn configures these with handlers of its own and ``propagate:
# False`` before it imports the app. They are walked either way — the
# walk is upstream of every handler — but they would come out in
# uvicorn's plain format beside the gateway's JSON, so an operator
# grepping one stream would find half of it in another shape. Superseded
# here, the way the backend supersedes them: the shared pre-chain
# reshapes an access line into named fields.
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


# The walk's own logger: records on it are skipped by the walk, by name,
# so a drop can never recurse. Not cached (see ``configure``), so this
# module-level binding picks up the stdlib factory once it is installed.
_walk_logger = structlog.get_logger(walkers.WALK_LOGGER)


def _drop(record: logging.LogRecord, **fields) -> None:
    """Say a record was dropped, and never raise into the caller doing
    it: this runs inside somebody's ``logger.info(...)``, and a logging
    call that throws turns a diagnostic into an outage."""
    try:
        _walk_logger.warning(
            "log_record_dropped", logger_name=record.name, count=1, **fields
        )
    except Exception:  # noqa: BLE001
        pass


# Set while this thread is inside the walk. A record emitted from in
# there — Presidio logs on every analyze call, spaCy on some — must not
# be walked, because walking it calls the redactor, which logs. The
# backend's listener thread carries the same guard under a different
# name (``_librerun_from_walk``); it is not walked either, and like the
# backend's it is still delivered: those lines are the redactor talking
# about recognizers, never about the text it was handed.
_walking = threading.local()


def _walking_call_handlers(self: logging.Logger, record: logging.LogRecord) -> None:
    if getattr(_walking, "active", False):
        _ORIGINAL_CALL_HANDLERS(self, record)
        return
    _walking.active = True
    try:
        # Four frames of ours between the caller and here: this
        # function, ``Logger.handle``, ``Logger._log`` and the
        # ``warning``/``info`` method itself.
        walkers.prepare_record(record, stack_skip=4)
        walkers.render_exceptions(record)
        walkers.walk_log_record(record)
    except walkers.RecordRefused as refused:
        _drop(
            record,
            kind=refused.kind,
            pii_type=refused.pii_type,
            path=refused.path,
        )
        return
    except Exception as exc:  # noqa: BLE001
        # Fail closed: a record the walk could not process is not shown.
        _drop(record, kind="walk_error", pii_type=type(exc).__name__)
        return
    finally:
        _walking.active = False
    _ORIGINAL_CALL_HANDLERS(self, record)


def install_walk() -> None:
    """Put the walk in front of every handler in the process. Idempotent."""
    global _walk_installed
    if _walk_installed:
        return
    logging.Logger.callHandlers = _walking_call_handlers  # type: ignore[method-assign]
    _walk_installed = True


def uninstall_walk() -> None:
    """Restore the stdlib's dispatch. For tests; nothing in the service
    calls it, because a gateway that stops walking is a gateway leaking."""
    global _walk_installed
    logging.Logger.callHandlers = _ORIGINAL_CALL_HANDLERS  # type: ignore[method-assign]
    _walk_installed = False


def _renderer():
    return structlog.processors.JSONRenderer()


def _level() -> int:
    """``LOG_LEVEL``, or INFO if it is not a level.

    ``setLevel`` raises on an unknown name, and this runs at import of
    ``gateway.main`` — a typo in one environment variable would stop the
    container from booting, which is a steep price for a volume dial.
    """
    name = (settings.LOG_LEVEL or "").strip().upper()
    resolved = logging.getLevelNamesMapping().get(name)
    if resolved is None:
        if name:
            _walk_logger.warning(
                "gateway_log_level_invalid", value=name, resolved="INFO"
            )
        return logging.INFO
    return resolved


def configure() -> None:
    """Install the walk, route structlog through stdlib, give the root a
    sink. Idempotent, and safe to call before the imports that log."""
    global _configured
    install_walk()
    if _configured:
        return

    structlog.configure(
        processors=[
            *SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Deliberately not cached. A cached proxy binds whatever factory
        # was configured at its first call, and every module here holds a
        # module-level ``get_logger`` built at import: one log line from
        # an import that beat ``configure()`` would pin that module to the
        # print factory for the life of the process, silently.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=[*STDLIB_PRE_CHAIN, *SHARED_PROCESSORS],
        pass_foreign_args=True,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            strip_positional_args,
            redact_user_content_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            _renderer(),
        ],
    )

    root = logging.getLogger()
    # A root that already has a handler keeps it and gets no second copy
    # of ours. In the container the root is empty, because this runs
    # before the imports that attach anything; under pytest it is the
    # capture handler, which must not be torn out from under the suite.
    # Either way the walk is upstream of every handler there is — that,
    # not an empty handler list, is what makes the sink safe.
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    root.setLevel(_level())
    for name, level in _LOGGER_LEVELS.items():
        logging.getLogger(name).setLevel(level)
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

    _configured = True


__all__ = ["configure", "install_walk", "uninstall_walk"]
